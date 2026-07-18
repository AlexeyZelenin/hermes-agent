"""Tests for hermes_cli.pocket_accounting — own per-pocket usage accounting.

Every test builds isolated session-log fixtures under ``tmp_path`` and an
in-memory sqlite ledger; nothing touches the operator's real ``~/.claude*`` dirs,
Codex home, or the live ``zeus.db``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hermes_cli import pocket_accounting as pa

WEEK_START = 1_784_000_000.0
NOW = WEEK_START + 4 * 3600  # 4h into the window


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --- Fixture builders --------------------------------------------------------


def _claude_session(project_dir, session_id: str, turns: list[tuple[float, dict]]):
    """Write one Claude Code session .jsonl with the given (ts, usage) turns."""
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for ts, usage in turns:
            fh.write(json.dumps({
                "type": "assistant", "sessionId": session_id,
                "timestamp": _iso(ts), "message": {"usage": usage},
            }) + "\n")
    return path


def _usage(inp=0, out=0, cr=0, cc=0) -> dict:
    return {
        "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc,
    }


def _codex_session(sessions_root, session_id: str, turns: list[tuple[float, int]]):
    """Write one Codex rollout .jsonl with token_count events."""
    day = sessions_root / "2026" / "05" / "14"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-05-14T20-43-11-{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n")
        for ts, tot in turns:
            fh.write(json.dumps({
                "type": "event_msg", "timestamp": _iso(ts),
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": tot},
                    "total_token_usage": {"total_tokens": tot * 9},
                }},
            }) + "\n")
    return path


def _ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE token_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
        " session_id TEXT DEFAULT '', subscription TEXT DEFAULT '', total_tokens INTEGER DEFAULT 0)"
    )
    return conn


def _add_ledger(conn, *, ts, session_id, subscription, total):
    conn.execute(
        "INSERT INTO token_usage (ts, session_id, subscription, total_tokens) VALUES (?,?,?,?)",
        (ts, session_id, subscription, total),
    )
    conn.commit()


# --- Token formula + readers -------------------------------------------------


def test_claude_turn_tokens_matches_ledger_formula():
    # in+out+cache_read+cache_create — verified against a real zeus.db row.
    assert pa._claude_turn_tokens(_usage(156, 70092, 8946911, 145777)) == 9162936


def test_claude_reader_sums_only_assistant_usage_turns(tmp_path):
    proj = tmp_path / "projects" / "-Users-x-repo"
    _claude_session(proj, "sid1", [
        (NOW - 3600, _usage(10, 20, 30, 40)),
        (NOW - 1800, _usage(1, 2, 3, 4)),
    ])
    turns = list(pa.iter_session_turns(str(tmp_path), "claude"))
    assert [t.tokens for t in turns] == [100, 10]
    assert all(t.session_id == "sid1" for t in turns)


def test_codex_reader_uses_last_turn_total(tmp_path):
    _codex_session(tmp_path / "sessions", "csid", [(NOW - 100, 12235), (NOW - 50, 500)])
    turns = list(pa.iter_session_turns(str(tmp_path), "codex"))
    assert [t.tokens for t in turns] == [12235, 500]
    assert all(t.session_id == "csid" for t in turns)


# --- Windowed accounting + dedup --------------------------------------------


def test_interactive_window_respects_bounds(tmp_path):
    proj = tmp_path / "projects" / "p"
    _claude_session(proj, "s", [
        (WEEK_START - 10, _usage(out=999)),   # before window: excluded
        (WEEK_START + 10, _usage(out=100)),   # in
        (NOW - 10, _usage(out=50)),           # in
        (NOW + 10, _usage(out=7)),            # after until: excluded
    ])
    res = pa.interactive_window_tokens(
        str(tmp_path), "claude", window_start=WEEK_START, until_ts=NOW)
    assert res == {"total_tokens": 150, "turns": 2, "sessions": 1}


def test_interactive_excludes_worker_sessions(tmp_path):
    proj = tmp_path / "projects" / "p"
    _claude_session(proj, "worker", [(NOW - 100, _usage(out=1000))])
    _claude_session(proj, "operator", [(NOW - 100, _usage(out=42))])
    res = pa.interactive_window_tokens(
        str(tmp_path), "claude", window_start=WEEK_START, until_ts=NOW,
        exclude_sessions=frozenset({"worker"}))
    assert res["total_tokens"] == 42
    assert res["sessions"] == 1


def test_attributed_session_ids_skips_empty_subscription():
    conn = _ledger()
    _add_ledger(conn, ts=NOW, session_id="w1", subscription="work2", total=5)
    _add_ledger(conn, ts=NOW, session_id="u1", subscription="", total=5)
    ids = pa.attributed_session_ids(conn)
    assert ids == {"w1"}  # '' is reclaimable, not excluded


def test_pocket_usage_combines_worker_and_interactive_no_double_count(tmp_path):
    # 'work2' pocket: one worker (ledger + its own log) and one operator session.
    conn = _ledger()
    _add_ledger(conn, ts=NOW - 200, session_id="worker", subscription="work2", total=9000)
    proj = tmp_path / "projects" / "p"
    _claude_session(proj, "worker", [(NOW - 200, _usage(out=8888))])   # already in ledger
    _claude_session(proj, "operator", [(NOW - 100, _usage(out=1234))])  # interactive
    usage = pa.pocket_usage(
        conn, "work2", str(tmp_path), "claude", window_start=WEEK_START, until_ts=NOW)
    assert usage["worker_tokens"] == 9000
    assert usage["interactive_tokens"] == 1234   # worker log NOT recounted
    assert usage["total_tokens"] == 10234
    assert usage["interactive_sessions"] == 1


def test_pocket_usage_reclaims_unattributed_ledger_session(tmp_path):
    # The 18.07 incident: a session logged under work2's dir but booked to ''.
    conn = _ledger()
    _add_ledger(conn, ts=NOW - 100, session_id="ghost", subscription="", total=1)
    proj = tmp_path / "projects" / "p"
    _claude_session(proj, "ghost", [(NOW - 100, _usage(out=5000))])
    usage = pa.pocket_usage(
        conn, "work2", str(tmp_path), "claude", window_start=WEEK_START, until_ts=NOW)
    # '' is not in any worker total, so the log reclaims it to work2.
    assert usage["worker_tokens"] == 0
    assert usage["interactive_tokens"] == 5000


def test_pocket_usage_no_config_dir_degrades_to_ledger(tmp_path):
    conn = _ledger()
    _add_ledger(conn, ts=NOW, session_id="w", subscription="work2", total=42)
    usage = pa.pocket_usage(conn, "work2", "", "claude", window_start=WEEK_START, until_ts=NOW)
    assert usage == {
        "subscription": "work2", "worker_tokens": 42, "interactive_tokens": 0,
        "total_tokens": 42, "interactive_sessions": 0,
    }


def test_missing_dir_yields_zero(tmp_path):
    res = pa.interactive_window_tokens(
        str(tmp_path / "nope"), "claude", window_start=WEEK_START, until_ts=NOW)
    assert res["total_tokens"] == 0


def test_file_cache_reuses_parse_until_mtime_changes(tmp_path):
    pa._FILE_CACHE.clear()
    proj = tmp_path / "projects" / "p"
    path = _claude_session(proj, "s", [(NOW - 100, _usage(out=10))])
    list(pa.iter_session_turns(str(tmp_path), "claude"))
    assert str(path) in pa._FILE_CACHE
    # Rewrite with more tokens but bump mtime so the cache invalidates.
    import os
    _claude_session(proj, "s", [(NOW - 100, _usage(out=99))])
    os.utime(path, (NOW + 10, NOW + 10))
    res = pa.interactive_window_tokens(
        str(tmp_path), "claude", window_start=WEEK_START, until_ts=NOW + 100)
    assert res["total_tokens"] == 99


# --- Calibration -------------------------------------------------------------


def test_reconcile_within_tolerance_is_none():
    assert pa.reconcile(52.0, 54.0, window_label="week", threshold_percent=3.0) is None


def test_reconcile_flags_undercount():
    div = pa.reconcile(51.0, 54.0, window_label="week", threshold_percent=2.0)
    assert div is not None
    assert div.diff_percent == -3.0
    assert div.window_label == "week"


def test_reconcile_missing_input_fails_open():
    assert pa.reconcile(None, 54.0, window_label="week") is None
    assert pa.reconcile(51.0, None, window_label="week") is None


def test_calibrate_emits_finding():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    finding = pa.calibrate(
        conn, "work2", window_label="5h", computed_percent=80.0,
        real_percent=90.0, board="ra", threshold_percent=3.0, now=NOW)
    assert finding is not None
    row = conn.execute(
        "SELECT source, severity, status FROM findings WHERE finding_key = ?",
        (pa.finding_key("work2", "5h"),)).fetchone()
    assert row["source"] == "pacing-calibration"
    assert row["severity"] == "warning"
    assert row["status"] == "open"


def test_calibrate_no_divergence_emits_nothing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert pa.calibrate(
        conn, "work2", window_label="5h", computed_percent=80.0,
        real_percent=80.5, now=NOW) is None


def test_calibrate_preserves_human_dismissal():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    args = dict(window_label="5h", computed_percent=80.0, real_percent=90.0, now=NOW)
    pa.calibrate(conn, "work2", **args)
    conn.execute("UPDATE findings SET status = 'dismissed'")
    pa.calibrate(conn, "work2", **args)  # re-emit must not reopen
    row = conn.execute("SELECT status FROM findings").fetchone()
    assert row["status"] == "dismissed"
