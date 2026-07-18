"""Regression tests for t_505eaa8f — dispatcher honours the operator's manual
card order (drag-rank) from the Zeus dashboard.

Drag-reordering in the dashboard persists a per-card ``rank`` (lower = earlier)
into zeus.db's ``task_flags`` table. The dispatcher previously claimed ready
tasks purely by ``priority DESC, created_at ASC``, ignoring that manual order.
It must now fold ``rank`` in as a tiebreak below priority and above created_at
(NULLS LAST), while degrading cleanly to the old order when no zeus ledger /
rank exists.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban board + a 'default' profile."""
    test_home = tempfile.mkdtemp(prefix="kanban_manual_rank_test_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


def _set_created_at(kb, ids):
    """Force distinct, monotonically increasing created_at so the baseline
    (no-rank) order is deterministic and not dependent on same-second ties."""
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            for i, tid in enumerate(ids):
                conn.execute(
                    "UPDATE tasks SET created_at = ? WHERE id = ?",
                    (1_000_000 + i, tid),
                )


def _write_zeus_ranks(home, ranks, board="default"):
    """Create a minimal zeus.db/task_flags carrying the given {task_id: rank}."""
    zeus_dir = os.path.join(home, "zeus")
    os.makedirs(zeus_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(zeus_dir, "zeus.db"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_flags ("
            "task_id TEXT PRIMARY KEY, board TEXT NOT NULL DEFAULT '', "
            "updated_at REAL NOT NULL DEFAULT 0, rank REAL)"
        )
        for tid, rank in ranks.items():
            conn.execute(
                "INSERT INTO task_flags(task_id, board, updated_at, rank) "
                "VALUES (?, ?, 0, ?)",
                (tid, board, rank),
            )
        conn.commit()
    finally:
        conn.close()


def _spawn_order(res):
    return [s[0] for s in res.spawned]


def test_no_zeus_ledger_falls_back_to_created_at(isolated_home):
    """Absent zeus.db: order is unchanged (priority DESC, created_at ASC)."""
    kb, _ = isolated_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [kb.create_task(conn, title=f"t{i}", assignee="default")
               for i in range(4)]
    _set_created_at(kb, ids)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert _spawn_order(res) == ids


def test_manual_rank_reorders_within_priority_band(isolated_home):
    """Ranks flip the claim order inside a single priority band."""
    kb, home = isolated_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [kb.create_task(conn, title=f"t{i}", assignee="default")
               for i in range(4)]
    _set_created_at(kb, ids)
    # Manual order: t3, t2, t0, t1 (lower rank = earlier).
    _write_zeus_ranks(home, {ids[3]: 1.0, ids[2]: 2.0, ids[0]: 3.0, ids[1]: 4.0})
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert _spawn_order(res) == [ids[3], ids[2], ids[0], ids[1]]


def test_unranked_cards_sort_after_ranked_nulls_last(isolated_home):
    """Cards without a manual rank fall to the end of their priority band."""
    kb, home = isolated_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [kb.create_task(conn, title=f"t{i}", assignee="default")
               for i in range(4)]
    _set_created_at(kb, ids)
    # Only t2 and t3 are dragged; t0/t1 keep no rank.
    _write_zeus_ranks(home, {ids[3]: 1.0, ids[2]: 2.0})
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    # Ranked first (t3, t2), then unranked in created_at order (t0, t1).
    assert _spawn_order(res) == [ids[3], ids[2], ids[0], ids[1]]


def test_priority_dominates_manual_rank(isolated_home):
    """A higher-priority card still claims first even with a worse rank."""
    kb, home = isolated_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        low = kb.create_task(conn, title="low-pri", assignee="default", priority=0)
        high = kb.create_task(conn, title="high-pri", assignee="default", priority=5)
    _set_created_at(kb, [low, high])
    # Give the low-priority card the best rank; priority must still win.
    _write_zeus_ranks(home, {low: 1.0, high: 999.0})
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert _spawn_order(res) == [high, low]


def test_rank_map_helper_degrades_without_ledger(isolated_home):
    """_zeus_rank_map returns {} (not raise) when zeus.db is absent."""
    kb, _ = isolated_home
    assert kb._zeus_rank_map(["t_abc", "t_def"]) == {}
    assert kb._zeus_rank_map([]) == {}
