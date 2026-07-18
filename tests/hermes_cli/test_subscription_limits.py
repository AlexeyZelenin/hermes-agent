"""Tests for hermes_cli.subscription_limits — empirical cap + vendor-shift gate.

The pure measurement/gate functions run on plain lists; the ledger reads and
findings push use an isolated in-memory sqlite mirroring the production zeus.db
tables. Nothing here touches a live ledger, board, or the operator's real
subscriptions.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import subscription_limits as sl

W = sl.SESSION_WINDOW_SECONDS
NOW = 1_784_400_000.0

# Only the columns the module reads; findings/measurement tables are created by
# the module itself via ensure_schema().
_LEDGER_SCHEMA = """
CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subscription TEXT NOT NULL DEFAULT '',
    total_tokens INTEGER NOT NULL DEFAULT 0
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEDGER_SCHEMA)
    sl.ensure_schema(conn)
    return conn


def _burn(conn: sqlite3.Connection, sub: str, ts: float, tokens: int) -> None:
    conn.execute(
        "INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, ?, ?)",
        (ts, sub, tokens),
    )


def _seed_measurements(
    conn: sqlite3.Connection, sub: str, values: list[int], *, base_ts: float = NOW
) -> None:
    """Insert measurements oldest→newest at monotonically increasing ts."""
    for i, v in enumerate(values):
        ts = base_ts + i
        conn.execute(
            "INSERT INTO subscription_limit_measurements "
            "(subscription, ts, window_start, window_reset, measured_tokens, turns) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sub, ts, ts - W, ts, v, 1),
        )


# ---------------------------------------------------------------------------
# detect_shift — the sustained-vs-jitter gate (pure)
# ---------------------------------------------------------------------------


def test_detect_shift_needs_enough_history():
    # baseline_min=3 + sustained=2 -> need >=5 points.
    assert sl.detect_shift([100, 100, 100, 100]) is None


def test_detect_shift_stable_series_no_flag():
    assert sl.detect_shift([100, 105, 98, 102, 100, 101]) is None


def test_detect_shift_single_outlier_is_jitter():
    # Only the very last window is high; the one before it is normal -> the
    # sustained run (last 2) is not all past threshold -> no flag.
    assert sl.detect_shift([100, 100, 100, 100, 150]) is None


def test_detect_shift_sustained_up_flags():
    v = sl.detect_shift([100, 100, 100, 150, 155])
    assert v is not None
    assert v.direction == "up"
    assert v.baseline == 100.0
    assert v.recent_median == pytest.approx(152.5)
    assert v.shift_percent == pytest.approx(52.5)


def test_detect_shift_sustained_down_flags():
    v = sl.detect_shift([200, 200, 200, 150, 150])
    assert v is not None
    assert v.direction == "down"
    assert v.shift_percent == pytest.approx(-25.0)


def test_detect_shift_recent_run_must_agree_on_direction():
    # One recent up, one recent down -> not the same side -> no flag.
    assert sl.detect_shift([100, 100, 100, 150, 60]) is None


def test_detect_shift_within_threshold_no_flag():
    # 10% move, below the 15% default threshold.
    assert sl.detect_shift([100, 100, 100, 110, 110]) is None


def test_detect_shift_respects_custom_threshold():
    v = sl.detect_shift([100, 100, 100, 110, 112], threshold_percent=5.0)
    assert v is not None and v.direction == "up"


# ---------------------------------------------------------------------------
# record_measurement — the window sum on a limit-hit (IO)
# ---------------------------------------------------------------------------


def test_record_measurement_sums_the_window_only():
    conn = _conn()
    reset = NOW
    _burn(conn, "personal", NOW - W - 10, 999)   # before window -> excluded
    _burn(conn, "personal", NOW - W + 100, 30_000)
    _burn(conn, "personal", NOW - 50, 20_000)
    _burn(conn, "other", NOW - 40, 77_000)       # other pocket -> excluded
    m = sl.record_measurement(conn, "personal", reset_at=reset, now=NOW)
    assert m is not None
    assert m["measured_tokens"] == 50_000
    assert m["turns"] == 2
    assert m["window_start"] == reset - W
    stored = conn.execute(
        "SELECT measured_tokens FROM subscription_limit_measurements"
    ).fetchall()
    assert [r["measured_tokens"] for r in stored] == [50_000]


def test_record_measurement_skips_zero_external_only_window():
    conn = _conn()
    # No Hermes burn in the window -> all spend was external -> not recorded.
    m = sl.record_measurement(conn, "personal", reset_at=NOW, now=NOW)
    assert m is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM subscription_limit_measurements"
    ).fetchone()["n"] == 0


def test_record_measurement_no_ledger_degrades():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sl.ensure_schema(conn)  # measurement + findings tables, but NO token_usage
    assert sl.record_measurement(conn, "personal", reset_at=NOW, now=NOW) is None


# ---------------------------------------------------------------------------
# series + pacing feed
# ---------------------------------------------------------------------------


def test_load_series_oldest_first_and_capped():
    conn = _conn()
    _seed_measurements(conn, "personal", list(range(1, 31)))
    series = sl.load_series(conn, "personal", lookback=5)
    assert series == [26, 27, 28, 29, 30]


def test_measured_session_limit_median_of_recent():
    conn = _conn()
    _seed_measurements(conn, "personal", [100, 200, 300, 400, 500])
    # median of the last 5.
    assert sl.measured_session_limit(conn, "personal") == 300.0


def test_measured_session_limit_min_samples_gate():
    conn = _conn()
    _seed_measurements(conn, "personal", [100])
    assert sl.measured_session_limit(conn, "personal") is None


def test_measured_session_limit_none_conn():
    assert sl.measured_session_limit(None, "personal") is None


# ---------------------------------------------------------------------------
# emit_finding — browse-only contract
# ---------------------------------------------------------------------------


def test_emit_finding_inserts_open_and_upserts():
    conn = _conn()
    v = sl.detect_shift([100, 100, 100, 150, 155])
    finding = sl.finding_for_shift("personal", v)
    sl.emit_finding(conn, finding, board="", now=NOW)
    row = conn.execute(
        "SELECT source, status, title, evidence_json FROM findings"
    ).fetchone()
    assert row["source"] == sl.FINDINGS_SOURCE
    assert row["status"] == "open"
    assert "personal" in row["title"]
    assert json.loads(row["evidence_json"])["direction"] == "up"
    # Re-emit refreshes, does not duplicate.
    sl.emit_finding(conn, finding, board="", now=NOW + 100)
    assert conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"] == 1


def test_emit_finding_preserves_human_dismissal():
    conn = _conn()
    v = sl.detect_shift([100, 100, 100, 150, 155])
    finding = sl.finding_for_shift("personal", v)
    sl.emit_finding(conn, finding, now=NOW)
    conn.execute("UPDATE findings SET status='dismissed'")
    sl.emit_finding(conn, finding, now=NOW + 100)  # re-scan must not resurface
    assert conn.execute("SELECT status FROM findings").fetchone()["status"] == "dismissed"


# ---------------------------------------------------------------------------
# on_limit_hit — end to end
# ---------------------------------------------------------------------------


def test_on_limit_hit_records_but_no_finding_without_shift():
    conn = _conn()
    _burn(conn, "personal", NOW - 100, 40_000)
    finding = sl.on_limit_hit(conn, "personal", reset_at=NOW, now=NOW)
    assert finding is None  # first measurement -> nothing to compare
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM subscription_limit_measurements"
    ).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"] == 0


def test_on_limit_hit_flags_sustained_shift_into_problems():
    conn = _conn()
    # Prior stable baseline of measurements (well before the limit-hits below),
    # then two high limit-hits.
    _seed_measurements(conn, "personal", [100_000, 100_000, 100_000],
                       base_ts=NOW - 10_000)
    # First high window: sustained run is [100k, 150k] -> not both high -> no flag.
    _burn(conn, "personal", NOW - 100, 150_000)
    assert sl.on_limit_hit(conn, "personal", reset_at=NOW, now=NOW) is None
    # Second high window: now the recent run is [150k, 155k] -> sustained up.
    _burn(conn, "personal", NOW + W - 100, 155_000)
    finding = sl.on_limit_hit(conn, "personal", reset_at=NOW + W, now=NOW + W)
    assert finding is not None
    assert finding["finding_key"] == "sublimit-shift:personal"
    row = conn.execute(
        "SELECT source, status FROM findings WHERE finding_key = ?",
        (finding["finding_key"],),
    ).fetchone()
    assert row["source"] == sl.FINDINGS_SOURCE and row["status"] == "open"
