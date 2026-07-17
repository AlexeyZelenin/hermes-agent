"""Tests for hermes_cli.regular_crons — the "Регулярные" cron registry.

Covers the two-axis classifiers (cadence / purpose), the cadence badge,
anomaly detectors (failure streak / token spike), run-history parsing, row
assembly, and the findings push (upsert / clear / dismiss-preserving). Each
test that needs a store uses an isolated temp dir — nothing touches a live
cron store or zeus ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import regular_crons as rc


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("zeus-nightly-reflection", rc.PURPOSE_REFLECTION),
    ("hourly-limits-probe", rc.PURPOSE_RESOURCES),
    ("token-replenishment", rc.PURPOSE_RESOURCES),
    ("nightly-security-review", rc.PURPOSE_SECURITY),
    ("5-min-health-check", rc.PURPOSE_SUPERVISION),
    ("supervisory-tick", rc.PURPOSE_SUPERVISION),
    ("zeus-db-backup", rc.PURPOSE_SUPERVISION),
    ("alpha", rc.PURPOSE_OTHER),
])
def test_classify_purpose(name, expected):
    assert rc.classify_purpose({"name": name}) == expected


def test_classify_purpose_priority_security_over_supervision():
    # "audit" (security) beats "check" (supervision) when both could match.
    assert rc.classify_purpose({"name": "security audit check"}) == rc.PURPOSE_SECURITY


def test_classify_purpose_uses_script_and_prompt():
    assert rc.classify_purpose({"name": "x", "script": "vuln-scan.py"}) == rc.PURPOSE_SECURITY
    assert rc.classify_purpose({"name": "x", "prompt": "reflect on today"}) == rc.PURPOSE_REFLECTION


@pytest.mark.parametrize("kind,expected", [
    ("interval", rc.CADENCE_INTERVAL),
    ("cron", rc.CADENCE_CALENDAR),
    ("once", rc.CADENCE_ONCE),
    ("weird", rc.CADENCE_UNKNOWN),
])
def test_classify_cadence(kind, expected):
    assert rc.classify_cadence({"schedule": {"kind": kind}}) == expected


@pytest.mark.parametrize("job,expected", [
    ({"schedule": {"kind": "interval", "minutes": 5}}, "каждые 5м"),
    ({"schedule": {"kind": "interval", "minutes": 120}}, "каждые 2ч"),
    ({"schedule": {"kind": "cron", "expr": "0 3 * * *"}}, "ежедневно 03:00"),
    ({"schedule": {"kind": "cron", "expr": "17 3 * * *"}}, "ежедневно 03:17"),
    # non-daily cron falls back to the raw display
    ({"schedule": {"kind": "cron", "expr": "0 9 1,15 * *"},
      "schedule_display": "0 9 1,15 * *"}, "0 9 1,15 * *"),
])
def test_cadence_badge(job, expected):
    assert rc.cadence_badge(job) == expected


# --- anomaly detectors ------------------------------------------------------


def test_failure_streak_counts_leading_errors():
    assert rc.detect_failure_streak(["error", "error", "error", "ok"]) == 3
    assert rc.detect_failure_streak(["error", "error", "error", "error"]) == 4


def test_failure_streak_below_threshold_is_zero():
    assert rc.detect_failure_streak(["error", "error", "ok"]) == 0  # streak 2 < 3
    assert rc.detect_failure_streak(["ok", "error", "error", "error"]) == 0  # newest ok


def test_failure_streak_custom_threshold():
    assert rc.detect_failure_streak(["error", "error"], threshold=2) == 2


def test_token_spike_flags_multiple_of_baseline():
    spike = rc.detect_token_spike([100, 110, 90, 600])
    assert spike is not None
    assert spike["latest"] == 600
    assert spike["baseline"] == 100.0  # median of [100, 110, 90]
    assert spike["factor"] == 6.0


def test_token_spike_none_when_within_range():
    assert rc.detect_token_spike([100, 110, 90, 150]) is None


def test_token_spike_needs_enough_history():
    # 2 prior + latest < min_history(3)+1 -> no baseline yet.
    assert rc.detect_token_spike([100, 100, 250]) is None


def test_token_spike_zero_baseline_safe():
    assert rc.detect_token_spike([0, 0, 0, 500]) is None  # no divide-by-zero


# --- run-history parsing ----------------------------------------------------


def test_recent_run_outcomes_reads_newest_first(tmp_path):
    job_dir = tmp_path / "jobA"
    job_dir.mkdir()
    (job_dir / "2026-07-01_04-00-00.md").write_text("# Cron Job: a\n\nok output")
    (job_dir / "2026-07-02_04-00-00.md").write_text("# Cron Job: a (FAILED)\n\n## Error\n```\nx\n```")
    (job_dir / "2026-07-03_04-00-00.md").write_text("# Cron Job: a (FAILED)\n\n## Error\n```\ny\n```")
    outcomes = rc.recent_run_outcomes("jobA", tmp_path)
    assert outcomes == ["error", "error", "ok"]  # newest first


def test_recent_run_outcomes_missing_dir_is_empty(tmp_path):
    assert rc.recent_run_outcomes("nope", tmp_path) == []


# --- row assembly -----------------------------------------------------------


def test_build_row_shape_and_tokens():
    job = {"id": "abc", "name": "health-check", "enabled": True, "state": "scheduled",
           "schedule": {"kind": "interval", "minutes": 5}, "last_status": "ok"}
    row = rc.build_row(job, token_stats={"total_tokens": 12500, "run_count": 3,
                                         "cost_usd": 0.5}, period_days=30,
                       failure_streak=0, token_spike=None)
    assert row["purpose"] == rc.PURPOSE_SUPERVISION
    assert row["cadence"] == rc.CADENCE_INTERVAL
    assert row["cadence_badge"] == "каждые 5м"
    assert row["enabled"] is True
    assert row["tokens"]["total_tokens"] == 12500
    assert row["tokens"]["display"] == "12.5K"
    assert row["tokens"]["period_days"] == 30
    assert row["anomalies"] == []


def test_build_row_carries_anomalies():
    row = rc.build_row({"id": "abc", "name": "x", "schedule": {"kind": "cron", "expr": "0 4 * * *"}},
                       failure_streak=4, token_spike={"latest": 900, "baseline": 100.0, "factor": 9.0})
    kinds = {a["kind"] for a in row["anomalies"]}
    assert kinds == {"consecutive_failures", "token_spike"}


def test_build_row_no_token_stats_omits_tokens():
    row = rc.build_row({"id": "abc", "name": "x", "no_agent": True,
                        "schedule": {"kind": "cron", "expr": "0 3 * * *"}})
    assert row["tokens"] is None


# --- findings push ----------------------------------------------------------


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "zeus.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _rows_with_anomaly(tmp_path):
    conn = _open_db(tmp_path)
    row = rc.build_row(
        {"id": "abc", "name": "flaky-cron", "last_error": "boom",
         "schedule": {"kind": "cron", "expr": "0 4 * * *"}},
        failure_streak=3,
    )
    return conn, [row]


def test_scan_and_emit_creates_finding(tmp_path):
    conn, rows = _rows_with_anomaly(tmp_path)
    try:
        emitted = rc.scan_and_emit(rows, conn, board="ra", now=1000.0)
        assert len(emitted) == 1
        assert emitted[0]["kind"] == "consecutive_failures"
        got = conn.execute(
            "SELECT source, finding_key, category, severity, status, created_at "
            "FROM findings"
        ).fetchone()
        assert got["source"] == rc.FINDINGS_SOURCE
        assert got["finding_key"] == "cron:abc:consecutive_failures"
        assert got["category"] == "reliability"
        assert got["status"] == "open"
        assert got["created_at"] == 1000.0
    finally:
        conn.close()


def test_scan_and_emit_is_idempotent_and_preserves_created_at(tmp_path):
    conn, rows = _rows_with_anomaly(tmp_path)
    try:
        rc.scan_and_emit(rows, conn, board="ra", now=1000.0)
        rc.scan_and_emit(rows, conn, board="ra", now=2000.0)  # re-emit later
        rows_db = conn.execute("SELECT created_at, updated_at FROM findings").fetchall()
        assert len(rows_db) == 1                 # upsert, not duplicate
        assert rows_db[0]["created_at"] == 1000.0  # preserved
        assert rows_db[0]["updated_at"] == 2000.0  # refreshed
    finally:
        conn.close()


def test_scan_and_emit_clears_when_recovered(tmp_path):
    conn, rows = _rows_with_anomaly(tmp_path)
    try:
        rc.scan_and_emit(rows, conn, board="ra", now=1000.0)
        # next scan: the same cron, now healthy (no anomalies)
        healthy = rc.build_row({"id": "abc", "name": "flaky-cron",
                                "schedule": {"kind": "cron", "expr": "0 4 * * *"}})
        rc.scan_and_emit([healthy], conn, board="ra", now=3000.0)
        status = conn.execute("SELECT status FROM findings").fetchone()["status"]
        assert status == "obsolete"
    finally:
        conn.close()


def test_scan_does_not_undismiss(tmp_path):
    conn, rows = _rows_with_anomaly(tmp_path)
    try:
        rc.scan_and_emit(rows, conn, board="ra", now=1000.0)
        conn.execute("UPDATE findings SET status='dismissed'")
        conn.commit()
        rc.scan_and_emit(rows, conn, board="ra", now=2000.0)  # still anomalous
        status = conn.execute("SELECT status FROM findings").fetchone()["status"]
        assert status == "dismissed"  # human's decision respected
    finally:
        conn.close()


def test_scan_and_emit_none_conn_is_noop(tmp_path):
    _, rows = _rows_with_anomaly(tmp_path)
    assert rc.scan_and_emit(rows, None, board="ra") == []


# --- integration: registry_for_jobs over a temp cron store + ledger ---------


def test_registry_for_jobs_end_to_end(tmp_path):
    zdb = tmp_path / "zeus.db"
    conn = sqlite3.connect(str(zdb))
    conn.row_factory = sqlite3.Row  # matches zeus_tokens.connect()'s contract
    conn.executescript(
        "CREATE TABLE token_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
        "session_id TEXT DEFAULT '', total_tokens INT DEFAULT 0, cost_usd REAL, "
        "prompt_tokens INT DEFAULT 0, completion_tokens INT DEFAULT 0);"
    )
    conn.executemany(
        "INSERT INTO token_usage (session_id, ts, total_tokens) VALUES (?, ?, ?)",
        [("cron_j1_20260101_010000", 10.0, 400), ("cron_j1_20260102_010000", 20.0, 500)],
    )
    conn.commit()

    jobs = [
        {"id": "j1", "name": "health-check", "enabled": True,
         "schedule": {"kind": "interval", "minutes": 5}},
        {"id": "j2", "name": "zeus-db-backup", "enabled": True, "no_agent": True,
         "schedule": {"kind": "cron", "expr": "17 3 * * *"}},
    ]
    try:
        rows = rc.registry_for_jobs(jobs, zeus_conn=conn, output_dir=tmp_path / "out",
                                    period_days=365, now=100.0)
    finally:
        conn.close()

    by_id = {r["id"]: r for r in rows}
    assert by_id["j1"]["tokens"]["total_tokens"] == 900
    assert by_id["j1"]["tokens"]["run_count"] == 2
    assert by_id["j1"]["cadence"] == rc.CADENCE_INTERVAL
    assert by_id["j2"]["tokens"] is None  # script cron, no ledger rows
    assert by_id["j2"]["cadence_badge"] == "ежедневно 03:17"
