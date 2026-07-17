"""Tests for hermes_cli.zeus_tokens — the read-only zeus token-ledger view.

Each test builds an isolated temp zeus.db mirroring the production
``~/.hermes/zeus/zeus.db`` schema; nothing here touches a live ledger or board.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import zeus_tokens

# Mirrors the production token_usage schema (~/.hermes/zeus/zeus.db).
_SCHEMA = """
CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    subscription TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    context_used INTEGER,
    context_size INTEGER,
    cost_usd REAL
);
CREATE INDEX idx_usage_task ON token_usage(task_id);
"""


def _make_zeus(path: Path, rows=()) -> Path:
    """Create a zeus.db at ``path`` and insert ``rows``.

    Each row is ``(task_id, ts, prompt, completion, total, cost_usd)``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO token_usage "
        "(task_id, ts, prompt_tokens, completion_tokens, total_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        list(rows),
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Path resolution + connect
# ---------------------------------------------------------------------------


def test_default_path_honours_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert zeus_tokens.default_zeus_db_path() == tmp_path / "h" / "zeus" / "zeus.db"


def test_connect_missing_db_returns_none(tmp_path):
    assert zeus_tokens.connect(tmp_path / "nope.db") is None


def test_connect_existing_db(tmp_path):
    db = _make_zeus(tmp_path / "zeus.db")
    conn = zeus_tokens.connect(db)
    try:
        assert conn is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# aggregate_by_task
# ---------------------------------------------------------------------------


def test_aggregate_sums_and_groups(tmp_path):
    db = _make_zeus(
        tmp_path / "zeus.db",
        rows=[
            ("t_a", 1.0, 100, 50, 150, 0.10),
            ("t_a", 2.0, 200, 100, 300, 0.20),  # second turn on same card
            ("t_b", 3.0, 10, 5, 15, None),
        ],
    )
    conn = zeus_tokens.connect(db)
    try:
        agg = zeus_tokens.aggregate_by_task(conn, ["t_a", "t_b", "t_missing"])
    finally:
        conn.close()
    assert agg["t_a"]["total_tokens"] == 450
    assert agg["t_a"]["prompt_tokens"] == 300
    assert agg["t_a"]["completion_tokens"] == 150
    assert agg["t_a"]["cost_usd"] == pytest.approx(0.30)
    assert agg["t_a"]["last_ts"] == 2.0
    assert agg["t_b"]["total_tokens"] == 15
    assert agg["t_b"]["cost_usd"] is None  # never priced
    assert "t_missing" not in agg  # no ledger rows -> omitted


def test_aggregate_missing_table_is_empty(tmp_path):
    # A zeus.db that exists but lacks the token_usage table (plugin half-set-up).
    db = tmp_path / "zeus.db"
    sqlite3.connect(str(db)).close()
    conn = zeus_tokens.connect(db)
    try:
        assert zeus_tokens.aggregate_by_task(conn, ["t_a"]) == {}
    finally:
        conn.close()


def test_aggregate_none_conn_and_empty_ids(tmp_path):
    assert zeus_tokens.aggregate_by_task(None, ["t_a"]) == {}
    db = _make_zeus(tmp_path / "zeus.db", rows=[("t_a", 1.0, 1, 1, 2, None)])
    conn = zeus_tokens.connect(db)
    try:
        assert zeus_tokens.aggregate_by_task(conn, []) == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# aggregate_by_cron / per_run_token_totals  (session-prefix attribution)
# ---------------------------------------------------------------------------


def _make_zeus_sessions(path: Path, rows=()) -> Path:
    """zeus.db with ``(session_id, ts, total, cost)`` rows for cron tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO token_usage (session_id, ts, total_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?)",
        list(rows),
    )
    conn.commit()
    conn.close()
    return path


def test_aggregate_by_cron_groups_by_session_prefix(tmp_path):
    # Two runs of job "abc" (two turns each) plus an unrelated job "def".
    db = _make_zeus_sessions(tmp_path / "zeus.db", rows=[
        ("cron_abc_20260101_010000", 10.0, 100, 0.01),
        ("cron_abc_20260101_010000", 11.0, 50, 0.005),
        ("cron_abc_20260102_010000", 20.0, 200, 0.02),
        ("cron_def_20260101_010000", 30.0, 999, 0.10),
    ])
    conn = zeus_tokens.connect(db)
    try:
        agg = zeus_tokens.aggregate_by_cron(conn, ["abc", "def", "ghi"])
    finally:
        conn.close()
    assert agg["abc"]["total_tokens"] == 350  # 100 + 50 + 200
    assert agg["abc"]["run_count"] == 2        # two distinct sessions
    assert agg["abc"]["cost_usd"] == pytest.approx(0.035)
    assert agg["abc"]["last_ts"] == 20.0
    assert agg["def"]["total_tokens"] == 999
    assert "ghi" not in agg  # no run sessions -> omitted


def test_aggregate_by_cron_since_ts_window(tmp_path):
    db = _make_zeus_sessions(tmp_path / "zeus.db", rows=[
        ("cron_abc_old", 100.0, 500, None),
        ("cron_abc_new", 200.0, 40, None),
    ])
    conn = zeus_tokens.connect(db)
    try:
        agg = zeus_tokens.aggregate_by_cron(conn, ["abc"], since_ts=150.0)
    finally:
        conn.close()
    assert agg["abc"]["total_tokens"] == 40   # old run excluded by window
    assert agg["abc"]["run_count"] == 1


def test_aggregate_by_cron_degrades(tmp_path):
    assert zeus_tokens.aggregate_by_cron(None, ["abc"]) == {}
    db = _make_zeus_sessions(tmp_path / "zeus.db")
    conn = zeus_tokens.connect(db)
    try:
        assert zeus_tokens.aggregate_by_cron(conn, []) == {}
    finally:
        conn.close()


def test_per_run_token_totals_ordered_oldest_first(tmp_path):
    db = _make_zeus_sessions(tmp_path / "zeus.db", rows=[
        ("cron_abc_r2", 20.0, 200, None),
        ("cron_abc_r1", 10.0, 100, None),
        ("cron_abc_r1", 11.0, 5, None),   # second turn of the older run
        ("cron_abc_r3", 30.0, 600, None),
    ])
    conn = zeus_tokens.connect(db)
    try:
        totals = zeus_tokens.per_run_token_totals(conn, "abc")
    finally:
        conn.close()
    assert totals == [105, 200, 600]  # r1(100+5), r2, r3 — chronological
    assert zeus_tokens.per_run_token_totals(None, "abc") == []


# ---------------------------------------------------------------------------
# graph helpers
# ---------------------------------------------------------------------------


def test_build_children_map_and_descendants():
    links = [("epic", "c1"), ("epic", "c2"), ("c1", "gc1")]
    cm = zeus_tokens.build_children_map(links)
    assert cm == {"epic": ["c1", "c2"], "c1": ["gc1"]}
    assert zeus_tokens.descendants("epic", cm) == {"c1", "c2", "gc1"}
    assert zeus_tokens.descendants("gc1", cm) == set()


def test_descendants_is_cycle_safe():
    # Pathological cycle a->b->a; must terminate and not include the root.
    cm = zeus_tokens.build_children_map([("a", "b"), ("b", "a")])
    assert zeus_tokens.descendants("a", cm) == {"b"}


# ---------------------------------------------------------------------------
# token_cost shaping
# ---------------------------------------------------------------------------


def test_token_cost_leaf_own_only():
    per = {"t_a": {"total_tokens": 150, "prompt_tokens": 100,
                   "completion_tokens": 50, "cost_usd": 0.1, "last_ts": 2.0}}
    tc = zeus_tokens.token_cost("t_a", per, descendant_ids=[])
    assert tc["own"]["total_tokens"] == 150
    assert "rollup" not in tc  # a leaf has no epic rollup


def test_token_cost_epic_rollup():
    per = {
        "epic": {"total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40,
                 "cost_usd": 0.10, "last_ts": 1.0},
        "c1": {"total_tokens": 300, "prompt_tokens": 200, "completion_tokens": 100,
               "cost_usd": 0.30, "last_ts": 2.0},
        "c2": {"total_tokens": 50, "prompt_tokens": 30, "completion_tokens": 20,
               "cost_usd": None, "last_ts": 3.0},
    }
    tc = zeus_tokens.token_cost("epic", per, descendant_ids=["c1", "c2"])
    assert tc["own"]["total_tokens"] == 100
    assert tc["rollup"]["total_tokens"] == 450  # 100 + 300 + 50
    assert tc["rollup"]["cost_usd"] == pytest.approx(0.40)  # None cost skipped
    assert tc["rollup"]["task_count"] == 3


def test_token_cost_epic_with_no_own_spend():
    # An orchestrator card that itself burned nothing but whose children did:
    # rollup still reports, own is absent.
    per = {"c1": {"total_tokens": 300, "prompt_tokens": 200,
                  "completion_tokens": 100, "cost_usd": 0.3, "last_ts": 2.0}}
    tc = zeus_tokens.token_cost("epic", per, descendant_ids=["c1"])
    assert "own" not in tc
    assert tc["rollup"]["total_tokens"] == 300
    assert tc["rollup"]["task_count"] == 1


def test_token_cost_none_when_no_data():
    assert zeus_tokens.token_cost("t_x", {}, descendant_ids=["c1"]) is None


# ---------------------------------------------------------------------------
# humanize_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,expected", [
    (0, "0"),
    (999, "999"),
    (1000, "1K"),
    (1500, "1.5K"),
    (409600, "409.6K"),
    (410_000, "410K"),
    (1_000_000, "1M"),
    (1_500_000, "1.5M"),
    (400_000, "400K"),
])
def test_humanize_tokens(n, expected):
    assert zeus_tokens.humanize_tokens(n) == expected
