"""Tests for hermes_cli.ecosystem_scout — the weekly ecosystem scan + install
effectiveness verdict (task t_bad68065).

The metric math is pure and runs on plain TaskMetric dicts (no host state). The
findings store uses an in-memory sqlite. The loader SQL runs against hand-built
in-memory tables. The cron-seeding tests isolate the cron store under a temp
HERMES_HOME so nothing touches the live board or the user's real cron jobs.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from hermes_cli import ecosystem_scout as es

DAY = es._SECONDS_PER_DAY


# ---------------------------------------------------------------------------
# Pure metric math
# ---------------------------------------------------------------------------


def _metric(task_id, done_ts, tokens=1000, attempts=1, duration_s=600):
    return {"task_id": task_id, "done_ts": done_ts, "tokens": tokens,
            "attempts": attempts, "duration_s": duration_s}


def test_window_stats_selects_window_and_aggregates():
    metrics = [
        _metric("a", 100, tokens=1000, attempts=1, duration_s=600),
        _metric("b", 150, tokens=3000, attempts=2, duration_s=1200),
        _metric("c", 999, tokens=9999, attempts=3, duration_s=9999),  # outside
    ]
    stats = es.window_stats(metrics, 0, 200)
    assert stats["n"] == 2
    assert stats["median_tokens"] == 2000  # median(1000, 3000)
    assert stats["retry_share"] == 0.5     # b retried
    assert stats["median_duration_s"] == 900


def test_window_stats_ignores_missing_token_and_duration_data():
    metrics = [
        _metric("a", 10, tokens=0, duration_s=0),      # missing both
        _metric("b", 20, tokens=500, duration_s=300),
    ]
    stats = es.window_stats(metrics, 0, 100)
    assert stats["n"] == 2                    # both counted for n / retry_share
    assert stats["median_tokens"] == 500      # zero-token task excluded
    assert stats["median_duration_s"] == 300


def test_window_stats_empty_window_is_all_none():
    stats = es.window_stats([], 0, 100)
    assert stats == {"n": 0, "median_tokens": None, "retry_share": None,
                     "median_duration_s": None}


def test_pct_change_handles_missing_and_zero_baseline():
    assert es._pct_change(100, 80) == pytest.approx(-0.2)
    assert es._pct_change(None, 80) is None
    assert es._pct_change(0, 80) is None
    assert es._pct_change(100, None) is None


def test_classify_verdict_insufficient_when_thin():
    thin = {"n": 1}
    deltas = {"tokens": -0.5, "retry_share": -0.5, "duration": -0.5}
    assert es.classify_verdict(thin, {"n": 50}, deltas) == es.VERDICT_INSUFFICIENT
    assert es.classify_verdict({"n": 50}, thin, deltas) == es.VERDICT_INSUFFICIENT


def test_classify_verdict_helped_hurt_neutral():
    big = {"n": 50}
    helped = {"tokens": -0.3, "retry_share": -0.3, "duration": 0.02}
    hurt = {"tokens": 0.3, "retry_share": 0.3, "duration": -0.02}
    neutral = {"tokens": -0.3, "retry_share": 0.3, "duration": 0.0}
    assert es.classify_verdict(big, big, helped) == es.VERDICT_HELPED
    assert es.classify_verdict(big, big, hurt) == es.VERDICT_HURT
    assert es.classify_verdict(big, big, neutral) == es.VERDICT_NEUTRAL


def test_compare_caps_after_window_at_now():
    install = 1000 * DAY
    now = install + 3 * DAY  # only 3 days of "after" exist, window is 14
    # 6 baseline tasks just before install, 6 after tasks within the 3 real days.
    # After improves tokens AND duration AND retries (2+ needed for a verdict).
    metrics = [_metric(f"b{i}", install - DAY, tokens=2000, attempts=2,
                       duration_s=1200) for i in range(6)]
    metrics += [_metric(f"a{i}", install + DAY, tokens=1000, attempts=1,
                        duration_s=600) for i in range(6)]
    # A task 10 days out would fall in the nominal window but past `now`.
    metrics.append(_metric("future", install + 10 * DAY, tokens=1))
    result = es.compare(metrics, install, window_days=14, now=now)
    assert result["baseline"]["n"] == 6
    assert result["after"]["n"] == 6  # future task excluded by the now cap
    assert result["verdict"] == es.VERDICT_HELPED  # tokens, duration, retries down


# ---------------------------------------------------------------------------
# Finding rendering + store
# ---------------------------------------------------------------------------


def test_finding_key_is_slugged_and_stable():
    assert es.finding_key("claude-mem v13.11") == "scout:verdict:claude-mem-v13-11"
    assert es.finding_key("!!!") == "scout:verdict:unknown"


def test_finding_for_renders_numbers_and_caveat():
    intervention = {"name": "Tokensave MCP", "installed_at": 1_000_000}
    result = {
        "verdict": es.VERDICT_HELPED, "window_days": 14,
        "baseline": {"n": 10, "median_tokens": 4000, "retry_share": 0.4,
                     "median_duration_s": 1200},
        "after": {"n": 12, "median_tokens": 2000, "retry_share": 0.2,
                  "median_duration_s": 600},
        "deltas": {"tokens": -0.5, "retry_share": -0.5, "duration": -0.5},
    }
    finding = es._finding_for(intervention, result)
    assert finding["finding_key"] == "scout:verdict:tokensave-mcp"
    assert finding["severity"] == "info"
    assert "помогло" in finding["title"]
    assert "-50%" in finding["detail"]
    assert "не доказательство причинности" in finding["detail"]


def test_finding_for_hurt_is_warning():
    intervention = {"name": "BadTool", "installed_at": 0}
    result = {"verdict": es.VERDICT_HURT, "window_days": 14,
              "baseline": {"n": 10, "median_tokens": 1, "retry_share": 0.1,
                           "median_duration_s": 1},
              "after": {"n": 10, "median_tokens": 2, "retry_share": 0.3,
                        "median_duration_s": 2},
              "deltas": {"tokens": 1.0, "retry_share": 2.0, "duration": 1.0}}
    assert es._finding_for(intervention, result)["severity"] == "warning"


def _memory_findings_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_emit_finding_upserts_and_preserves_dismissed():
    conn = _memory_findings_db()
    finding = {"finding_key": "scout:verdict:x", "title": "v1", "detail": "d1",
               "category": "ecosystem-effect", "severity": "info",
               "evidence": {"verdict": "neutral"}}
    es.emit_finding(conn, finding, board="ra", now=1.0)
    (row,) = conn.execute("SELECT title, status, created_at FROM findings").fetchall()
    assert row["title"] == "v1" and row["status"] == "open"

    # A human dismisses it; re-emitting refreshes text but keeps it dismissed.
    conn.execute("UPDATE findings SET status='dismissed' WHERE finding_key=?",
                 (finding["finding_key"],))
    conn.commit()
    finding["title"] = "v2"
    es.emit_finding(conn, finding, board="ra", now=2.0)
    (row2,) = conn.execute(
        "SELECT title, status, created_at, updated_at FROM findings").fetchall()
    assert row2["title"] == "v2"
    assert row2["status"] == "dismissed"       # not un-dismissed
    assert row2["created_at"] == 1.0           # preserved
    assert row2["updated_at"] == 2.0
    # Exactly one row — it upserted, not inserted a duplicate.
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1


def test_emit_finding_detail_is_valid_utf8_roundtrip():
    """Guard the class of crash that felled prior attempts: mojibake in a text
    column. A real Cyrillic detail must survive a strict-UTF-8 re-read."""
    conn = _memory_findings_db()
    finding = {"finding_key": "k", "title": "Вердикт «X»: помогло",
               "detail": "Токены/задача: 4 000 → 2 000. Наблюдение, не факт.",
               "category": "c", "severity": "info", "evidence": {"a": "тест"}}
    es.emit_finding(conn, finding, board="ra", now=1.0)
    (row,) = conn.execute("SELECT detail, evidence_json FROM findings").fetchall()
    assert "Токены" in row["detail"]
    assert "тест" in row["evidence_json"]  # ensure_ascii=False kept it readable


# ---------------------------------------------------------------------------
# run_verdict wiring
# ---------------------------------------------------------------------------


def test_run_verdict_emits_for_signal_and_skips_insufficient():
    conn = _memory_findings_db()
    install = 500 * DAY
    now = install + 20 * DAY
    metrics = [_metric(f"b{i}", install - DAY, tokens=4000, attempts=2,
                       duration_s=1200) for i in range(6)]
    metrics += [_metric(f"a{i}", install + DAY, tokens=2000, attempts=1,
                        duration_s=600) for i in range(6)]
    interventions = [
        {"name": "Good", "installed_at": install, "window_days": 14},
        {"name": "TooFresh", "installed_at": now - DAY, "window_days": 14},
    ]
    results = es.run_verdict(interventions=interventions, metrics=metrics,
                             conn=conn, now=now)
    by_name = {r["intervention"]["name"]: r["verdict"] for r in results}
    assert by_name["Good"] == es.VERDICT_HELPED
    assert by_name["TooFresh"] == es.VERDICT_INSUFFICIENT
    keys = [r["finding_key"] for r in
            conn.execute("SELECT finding_key FROM findings").fetchall()]
    assert keys == ["scout:verdict:good"]  # insufficient one was not pushed


def test_run_verdict_no_interventions_is_noop():
    assert es.run_verdict(interventions=[], metrics=[]) == []


# ---------------------------------------------------------------------------
# Interventions registry
# ---------------------------------------------------------------------------


def test_add_load_intervention_roundtrip_and_dedup(tmp_path):
    path = tmp_path / "interventions.json"
    es.add_intervention("claude-mem", 1000.0, window_days=7, note="upgrade",
                        path=path)
    es.add_intervention("Tokensave", 2000.0, path=path)
    items = es.load_interventions(path)
    assert {i["name"] for i in items} == {"claude-mem", "Tokensave"}

    # Re-adding the same name updates in place (no duplicate).
    es.add_intervention("claude-mem", 1500.0, path=path)
    items = es.load_interventions(path)
    assert len(items) == 2
    (cm,) = [i for i in items if i["name"] == "claude-mem"]
    assert cm["installed_at"] == 1500.0


def test_load_interventions_absent_file_is_empty(tmp_path):
    assert es.load_interventions(tmp_path / "nope.json") == []


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def test_parse_installed_at_epoch_date_and_default():
    assert es._parse_installed_at("1700000000", now=5.0) == 1_700_000_000.0
    assert es._parse_installed_at(None, now=5.0) == 5.0
    # A UTC calendar date round-trips through the UTC renderer to the same day.
    ts = es._parse_installed_at("2026-07-17", now=5.0)
    assert es.time.strftime("%Y-%m-%d", es.time.gmtime(ts)) == "2026-07-17"


def test_render_human_empty_and_populated():
    assert "нет отслеживаемых установок" in es._render_human([])
    results = [{"intervention": {"name": "X"}, "verdict": es.VERDICT_HELPED,
               "baseline": {"n": 10}, "after": {"n": 12}}]
    text = es._render_human(results)
    assert "«X»" in text and "помогло" in text


# ---------------------------------------------------------------------------
# Loaders (in-memory sqlite standing in for kanban DB)
# ---------------------------------------------------------------------------


def _kanban_like_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, started_at INT, "
                 "completed_at INT)")
    conn.execute("CREATE TABLE task_runs (task_id TEXT, status TEXT, "
                 "started_at INT)")
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
        ("done1", "done", 1000, 1600),      # duration 600
        ("done2", "done", 2000, 2000),      # zero duration -> None
        ("open1", "todo", 3000, None),      # not done -> excluded
        ("olddone", "done", 5, 55),         # before since_ts
    ])
    conn.executemany("INSERT INTO task_runs VALUES (?,?,?)", [
        ("done1", "done", 1000),
        ("done2", "crashed", 1900), ("done2", "done", 2000),  # 2 attempts
    ])
    return conn


def test_load_lifecycle_filters_done_and_computes_duration():
    conn = _kanban_like_conn()
    rows = es._load_lifecycle(conn, since_ts=100)
    by_id = {r["task_id"]: r for r in rows}
    assert set(by_id) == {"done1", "done2"}          # open + old excluded
    assert by_id["done1"]["duration_s"] == 600
    assert by_id["done2"]["duration_s"] is None       # zero collapses to None


def test_load_attempts_counts_runs():
    conn = _kanban_like_conn()
    attempts = es._load_attempts(conn)
    assert attempts == {"done1": 1, "done2": 2}


def test_load_task_metrics_merges_lifecycle_attempts_tokens(monkeypatch):
    conn = _kanban_like_conn()
    monkeypatch.setattr(es, "_load_tokens", lambda ids: {"done1": 1234})

    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "connect", lambda board=None: conn)

    metrics = es.load_task_metrics(board="ra", since_ts=100)
    by_id = {m["task_id"]: m for m in metrics}
    assert by_id["done1"]["tokens"] == 1234
    assert by_id["done1"]["attempts"] == 1
    assert by_id["done2"]["tokens"] == 0     # no ledger row -> 0
    assert by_id["done2"]["attempts"] == 2


# ---------------------------------------------------------------------------
# Cron seeding (isolated HERMES_HOME)
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    return home


def test_ensure_verdict_job_is_idempotent(hermes_env):
    import cron.jobs as jobs
    first = es.ensure_scout_verdict_job()
    assert first is not None
    assert first["no_agent"] is True
    assert (first.get("origin") or {}).get("kind") == "ecosystem-scout-verdict"
    assert (hermes_env / "scripts" / es._RUNNER_SCRIPT_NAME).exists()
    second = es.ensure_scout_verdict_job()
    assert second["id"] == first["id"]
    matches = [j for j in jobs.load_jobs()
               if (j.get("origin") or {}).get("kind") == "ecosystem-scout-verdict"]
    assert len(matches) == 1


def test_ensure_scan_job_is_agent_with_toolsets(hermes_env):
    import cron.jobs as jobs
    first = es.ensure_scout_scan_job()
    assert first is not None
    assert first["no_agent"] is False
    assert (first.get("origin") or {}).get("kind") == "ecosystem-scout-scan"
    assert set(first.get("enabled_toolsets") or []) >= {"web", "kanban"}
    # Second call short-circuits (no duplicate).
    second = es.ensure_scout_scan_job()
    assert second["id"] == first["id"]
    matches = [j for j in jobs.load_jobs()
               if (j.get("origin") or {}).get("kind") == "ecosystem-scout-scan"]
    assert len(matches) == 1
