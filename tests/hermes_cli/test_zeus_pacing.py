"""Tests for hermes_cli.zeus_pacing — the read-only pacing/limit view.

Each test builds an isolated in-memory sqlite mirroring the production
``~/.hermes/zeus/zeus.db`` pacing tables; nothing here touches a live ledger,
board, or the operator's real subscriptions.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import zeus_pacing

# Mirrors the production pacing/subscription/ledger schema (zeus.db).
_SCHEMA = """
CREATE TABLE pacing_state (
    subscription TEXT NOT NULL DEFAULT '',
    board TEXT NOT NULL DEFAULT '',
    window_label TEXT NOT NULL DEFAULT '',
    spent_percent REAL,
    target_percent REAL,
    elapsed_percent REAL,
    reset_at REAL,
    mode TEXT NOT NULL DEFAULT 'idle',
    agent_limit INTEGER,
    burn_rate_per_min REAL,
    reason TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (board, subscription)
);
CREATE TABLE claude_subscriptions (
    name TEXT PRIMARY KEY,
    config_dir TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    max_concurrency INTEGER NOT NULL DEFAULT 4,
    cooling_until REAL,
    last_limited_at REAL,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    subscription TEXT NOT NULL DEFAULT '',
    total_tokens INTEGER NOT NULL DEFAULT 0
);
"""

NOW = 1_784_400_000.0
WEEK = 7 * 24 * 3600


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _add_pacing(conn: sqlite3.Connection, **kw) -> None:
    row = {
        "subscription": "personal",
        "board": "ra",
        "window_label": "Current week",
        "spent_percent": 50.0,
        "target_percent": 40.0,
        "elapsed_percent": 50.0,
        "reset_at": NOW + WEEK / 2,
        "mode": "throttle",
        "agent_limit": 2,
        "burn_rate_per_min": 4000.0,
        "reason": "spending ahead",
        "updated_at": NOW,
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO pacing_state (subscription, board, window_label, spent_percent, "
        "target_percent, elapsed_percent, reset_at, mode, agent_limit, "
        "burn_rate_per_min, reason, updated_at) VALUES "
        "(:subscription, :board, :window_label, :spent_percent, :target_percent, "
        ":elapsed_percent, :reset_at, :mode, :agent_limit, :burn_rate_per_min, "
        ":reason, :updated_at)",
        row,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Degradation — absent conn / tables never raise
# ---------------------------------------------------------------------------

def test_snapshot_none_conn_is_empty():
    snap = zeus_pacing.pacing_snapshot(None, "ra", now=NOW)
    assert snap == {"board": "ra", "now": NOW, "pockets": [], "window_total_tokens": 0}


def test_snapshot_missing_pacing_table_is_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # no schema at all
    snap = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)
    assert snap["pockets"] == []
    assert snap["window_total_tokens"] == 0


# ---------------------------------------------------------------------------
# Basic pocket shaping
# ---------------------------------------------------------------------------

def test_pocket_core_fields_and_derived():
    conn = _conn()
    _add_pacing(conn)
    snap = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)
    assert len(snap["pockets"]) == 1
    p = snap["pockets"][0]
    assert p["subscription"] == "personal"
    assert p["mode"] == "throttle"
    assert p["agent_limit"] == 2
    # spent 50 vs target 40 -> 10 ahead of pace, not on track.
    assert p["pace_delta"] == 10.0
    assert p["on_track"] is False
    # reset is half a week out; countdown floors at 0 and is positive here.
    assert p["seconds_to_reset"] == pytest.approx(WEEK / 2)
    assert p["staleness_seconds"] == 0.0


def test_under_target_is_on_track():
    conn = _conn()
    _add_pacing(conn, spent_percent=20.0, target_percent=50.0, mode="idle")
    p = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)["pockets"][0]
    assert p["pace_delta"] == -30.0
    assert p["on_track"] is True


def test_board_filter_excludes_other_boards():
    conn = _conn()
    _add_pacing(conn, subscription="personal", board="ra")
    _add_pacing(conn, subscription="work1", board="other")
    snap = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)
    assert [p["subscription"] for p in snap["pockets"]] == ["personal"]


def test_reset_in_past_floors_countdown_to_zero():
    conn = _conn()
    _add_pacing(conn, reset_at=NOW - 100)
    p = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)["pockets"][0]
    assert p["seconds_to_reset"] == 0.0


# ---------------------------------------------------------------------------
# Subscription metadata enrichment
# ---------------------------------------------------------------------------

def test_subscription_meta_and_cooling():
    conn = _conn()
    _add_pacing(conn)
    conn.execute(
        "INSERT INTO claude_subscriptions (name, display_name, enabled, cooling_until, "
        "last_limited_at) VALUES ('personal', 'Personal Max', 1, ?, ?)",
        (NOW + 600, NOW - 60),
    )
    conn.commit()
    p = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)["pockets"][0]
    assert p["display_name"] == "Personal Max"
    assert p["enabled"] is True
    assert p["cooling"] is True  # cooling_until is in the future
    assert p["last_limited_at"] == NOW - 60


def test_display_name_falls_back_to_subscription():
    conn = _conn()
    _add_pacing(conn)  # no claude_subscriptions row
    p = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)["pockets"][0]
    assert p["display_name"] == "personal"
    assert p["cooling"] is False


# ---------------------------------------------------------------------------
# Window derivation + token totals
# ---------------------------------------------------------------------------

def test_window_start_derived_from_reset_and_elapsed():
    # elapsed 50% at updated_at, reset half a window out -> window is ~1 week,
    # so window_start ~= reset_at - week.
    ws = zeus_pacing._window_start(NOW + WEEK / 2, 50.0, NOW)
    assert ws == pytest.approx(NOW - WEEK / 2)


def test_window_start_fallback_on_degenerate_elapsed():
    reset = NOW + 1000
    assert zeus_pacing._window_start(reset, 0.0, NOW) == reset - zeus_pacing._DEFAULT_WINDOW_SECONDS
    assert zeus_pacing._window_start(reset, 100.0, NOW) == reset - zeus_pacing._DEFAULT_WINDOW_SECONDS
    assert zeus_pacing._window_start(None, 50.0, NOW) is None


def test_window_tokens_only_counts_in_window_and_subscription():
    conn = _conn()
    _add_pacing(conn)  # window_start ~= NOW - WEEK/2
    # In-window for personal.
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'personal', 100)", (NOW - 10,))
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'personal', 50)", (NOW - 20,))
    # Before the window start -> excluded.
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'personal', 999)", (NOW - WEEK,))
    # Different subscription -> excluded.
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'work1', 777)", (NOW - 5,))
    conn.commit()
    snap = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)
    p = snap["pockets"][0]
    assert p["window_tokens"]["total_tokens"] == 150
    assert p["window_tokens"]["turns"] == 2
    assert snap["window_total_tokens"] == 150


def test_window_total_tokens_sums_across_pockets():
    conn = _conn()
    _add_pacing(conn, subscription="personal")
    _add_pacing(conn, subscription="work1")
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'personal', 100)", (NOW - 10,))
    conn.execute("INSERT INTO token_usage (ts, subscription, total_tokens) VALUES (?, 'work1', 200)", (NOW - 10,))
    conn.commit()
    snap = zeus_pacing.pacing_snapshot(conn, "ra", now=NOW)
    assert snap["window_total_tokens"] == 300
