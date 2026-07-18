"""Lease saturation is 'wait for a slot', not 'pool exhausted' (t_4c4dbe64).

Two coupled fixes are exercised here, both LLM-free:

  1. The dispatcher sizes claude-code spawns to the subscription pool's free
     leases. When every live slot is busy (saturation) it DEFERS the ready
     task (``skipped_capacity``) instead of spawning a worker that would only
     park inside ``subs.acquire`` and then block the card as exhausted. A
     fully-cooling pool (genuine exhaustion) is left uncapped so it still
     reaches the block + auto-unblock path.

  2. ``requeue_capacity_deferred`` returns a running task to ``ready`` after it
     loses the lease race — no failure counted, no quota stamp, no ``blocked``
     round-trip — so it respawns cheaply once a slot frees.

The board and the zeus subscription DB are fully isolated under a temp
HERMES_HOME; nothing touches the live board or the real login keychain.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent import claude_subscriptions as subs
from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    """A private board + private zeus subscription DB under a temp HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The pool's login check shells out to the Keychain; force every pocket to
    # read as logged-in so capacity turns purely on leases/cooling, and keep
    # sync_registry from importing the developer's real ~/.claude dirs.
    monkeypatch.setattr(subs, "is_logged_in", lambda config_dir: True)
    monkeypatch.setattr(subs, "_discover_config_dirs", lambda: {})
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _seed_pocket(tmp_path, *, name="pocket", max_concurrency=1, cooling_until=None):
    config_dir = tmp_path / f".claude-sub-{name}"
    config_dir.mkdir(exist_ok=True)
    now = time.time()
    conn = subs.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO claude_subscriptions"
                " (name, config_dir, enabled, max_concurrency,"
                "  cooling_until, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                "   cooling_until = excluded.cooling_until,"
                "   max_concurrency = excluded.max_concurrency",
                (name, str(config_dir), max_concurrency, cooling_until, now, now),
            )
    finally:
        conn.close()
    return name


def _lease(name):
    """Hold a lease on ``name`` with THIS process's live pid so the pool's
    stale-lease GC keeps it (a dead pid would be reaped)."""
    conn = subs.connect()
    try:
        import os
        with conn:
            conn.execute(
                "INSERT INTO subscription_leases"
                " (subscription, task_id, pid, acquired_at)"
                " VALUES (?, '', ?, ?)",
                (name, os.getpid(), time.time()),
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# requeue_capacity_deferred
# --------------------------------------------------------------------------

def test_requeue_capacity_deferred_returns_task_to_ready(isolated_board):
    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        assert kb.claim_task(conn, task, claimer="host-1:1") is not None
        assert kb.get_task(conn, task).status == "running"

        ok = kb.requeue_capacity_deferred(conn, task, reason="pool saturated")
        assert ok is True

        t = kb.get_task(conn, task)
        assert t.status == "ready"
        assert t.claim_lock is None
        assert t.assignee == "worker"
        # Neutral outcome — NOT a failure, NOT blocked.
        assert t.consecutive_failures == 0
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task,),
        ).fetchone()
        assert run["outcome"] == "capacity_deferred"
        ev = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task,),
        ).fetchone()
        assert ev["kind"] == "capacity_deferred"


def test_requeue_capacity_deferred_no_respawn_guard(isolated_board):
    # A capacity-deferred task must be immediately re-spawnable (no quota
    # cooldown / blocker_auth stamp) — the lease cap, not the guard, holds it.
    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        kb.claim_task(conn, task, claimer="host-1:1")
        kb.requeue_capacity_deferred(conn, task, reason="pool saturated")
        assert kb.check_respawn_guard(conn, task) is None


def test_requeue_capacity_deferred_noop_when_not_running(isolated_board):
    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")  # stays 'todo'
        assert kb.requeue_capacity_deferred(conn, task, reason="x") is False


# --------------------------------------------------------------------------
# dispatcher lease cap
# --------------------------------------------------------------------------

def test_dispatcher_defers_claude_code_when_pool_saturated(
    isolated_board, tmp_path, all_assignees_spawnable
):
    # One pocket, one slot, already leased -> zero free. A ready claude-code
    # task must be deferred (skipped_capacity), NOT spawned.
    _seed_pocket(tmp_path, max_concurrency=1)
    _lease("pocket")
    assert subs.capacity_snapshot() == (1, 0)

    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task).status == "ready"

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == []
        assert task in res.skipped_capacity
        assert kb.get_task(conn, task).status == "ready"


def test_dispatcher_spawns_claude_code_when_slot_free(
    isolated_board, tmp_path, all_assignees_spawnable
):
    _seed_pocket(tmp_path, max_concurrency=1)  # one free slot, no lease
    assert subs.capacity_snapshot() == (1, 1)

    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        kb.recompute_ready(conn)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == [task]
        assert res.skipped_capacity == []
        assert kb.get_task(conn, task).status == "running"


def test_dispatcher_caps_to_free_slots_across_multiple_ready(
    isolated_board, tmp_path, all_assignees_spawnable
):
    # Two free slots, three ready claude-code tasks: spawn exactly two, defer
    # the third for a slot.
    _seed_pocket(tmp_path, max_concurrency=2)
    assert subs.capacity_snapshot() == (2, 2)

    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"cc{i}", assignee="worker",
                           executor="claude-code")
        kb.recompute_ready(conn)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=10)
        assert len(spawns) == 2
        assert len(res.skipped_capacity) == 1


def test_dispatcher_does_not_cap_non_claude_code(
    isolated_board, tmp_path, all_assignees_spawnable
):
    # A saturated Claude pool must not starve hermes-worker tasks — the cap is
    # scoped to the claude-code executor only.
    _seed_pocket(tmp_path, max_concurrency=1)
    _lease("pocket")

    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        task = kb.create_task(conn, title="hw", assignee="worker",
                              executor="hermes-worker")
        kb.recompute_ready(conn)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == [task]
        assert res.skipped_capacity == []


def test_dispatcher_does_not_cap_when_pool_empty(
    isolated_board, tmp_path, all_assignees_spawnable
):
    # No subscriptions registered -> executor uses the legacy single-session
    # fallback; the lease cap must NOT fire (pool_size == 0).
    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        kb.recompute_ready(conn)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == [task]
        assert res.skipped_capacity == []


def test_dispatcher_does_not_cap_when_pool_fully_cooling(
    isolated_board, tmp_path, all_assignees_spawnable
):
    # Every pocket cooling = genuine exhaustion (total capacity 0). The cap is
    # deliberately disabled so the worker still reaches the block +
    # auto-unblock path for operator visibility.
    _seed_pocket(tmp_path, max_concurrency=1, cooling_until=time.time() + 900)
    assert subs.capacity_snapshot() == (0, 0)

    spawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        spawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        task = kb.create_task(conn, title="cc", assignee="worker",
                              executor="claude-code")
        kb.recompute_ready(conn)
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == [task]
        assert res.skipped_capacity == []
