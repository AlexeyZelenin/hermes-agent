"""End-to-end proof of the deterministic auto-resume after a subscription
window reset (kanban task t_425e7727).

The whole chain is LLM-free and time-arithmetic only:

  1. A claude-code task dies because every subscription pocket is cooling on a
     usage limit -> the executor blocks it with ``kind='capability'`` and the
     :data:`SUBSCRIPTIONS_EXHAUSTED_MARKER` embedded in the reason.
  2. While at least one pocket is still cooling, ``dispatch_once`` must NOT
     unblock it (``pool_has_capacity()`` is False).
  3. Once the cooldown window lapses (the 5h reset "clock passes"), the very
     next ``dispatch_once`` tick calls ``_auto_unblock_subscription_blocked``,
     sees capacity, releases the task, and respawns it in the same tick.
  4. Across the whole block/unblock cycle the task keeps its assignee and its
     dependency edges intact.

The board is fully isolated (``HERMES_KANBAN_HOME``/``HERMES_HOME`` in a temp
dir); nothing touches the live kanban board or the live zeus subscription DB.
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
    # Path.home() feeds both the board root and the subscription pool's config
    # dir discovery; pin it at the temp dir so neither can see real logins.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _seed_cooling_pocket(tmp_path, cooling_until):
    """Register one enabled, logged-in subscription pocket in the isolated
    zeus DB with the given ``cooling_until`` timestamp. Returns its name."""
    config_dir = tmp_path / ".claude-sub-pocket"
    config_dir.mkdir(exist_ok=True)
    now = time.time()
    conn = subs.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO claude_subscriptions"
                " (name, config_dir, enabled, max_concurrency,"
                "  cooling_until, created_at, updated_at)"
                " VALUES ('pocket', ?, 1, 1, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET cooling_until = excluded.cooling_until",
                (str(config_dir), cooling_until, now, now),
            )
    finally:
        conn.close()
    return "pocket"


def _set_cooling_until(value):
    conn = subs.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE claude_subscriptions SET cooling_until = ? WHERE name = 'pocket'",
                (value,),
            )
    finally:
        conn.close()


def _block_event_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events"
        " WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["payload"] if row else None


def _parents_of(conn, task_id):
    return [
        r["parent_id"]
        for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        )
    ]


def test_auto_resume_after_subscription_window_reset(
    isolated_board, tmp_path, monkeypatch, all_assignees_spawnable
):
    # The pool's login check shells out to the Keychain; force the seeded
    # pocket to read as logged-in so capacity turns purely on the cooldown.
    monkeypatch.setattr(subs, "is_logged_in", lambda config_dir: True)

    future = time.time() + 3600  # pocket still cooling for another hour
    _seed_cooling_pocket(tmp_path, cooling_until=future)

    respawns: list[str] = []

    def fake_spawn(task, workspace_path, board=None):
        respawns.append(task.id)
        return 4321

    with kb.connect() as conn:
        # A done parent + a child that depends on it. The dependency edge and
        # the assignee must survive the whole block/unblock cycle (scope C).
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=[parent]
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        # Parent is done -> the machinery promotes the child todo -> ready.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"

        # Simulate the executor's limit-death block: capability kind + marker.
        assert kb.claim_task(conn, child, claimer="lock-1") is not None
        reason = (
            f"External claude-code ACP session failed: "
            f"{subs.SUBSCRIPTIONS_EXHAUSTED_MARKER} all Claude Code "
            f"subscriptions are cooling after usage limits"
        )
        assert kb.block_task(conn, child, reason=reason, kind="capability") is True

        blocked = kb.get_task(conn, child)
        assert blocked.status == "blocked"
        assert blocked.block_kind == "capability"
        assert blocked.assignee == "worker"
        assert _parents_of(conn, child) == [parent]
        assert subs.SUBSCRIPTIONS_EXHAUSTED_MARKER in (_block_event_payload(conn, child) or "")

        # --- Phase 1: pocket still cooling -> no capacity -> stays blocked ---
        assert subs.pool_has_capacity() is False
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert result.auto_unblocked == []
        assert respawns == []
        still = kb.get_task(conn, child)
        assert still.status == "blocked"
        assert still.assignee == "worker"

        # --- Phase 2: the 5h window resets (clock passes) -> auto-resume ---
        _set_cooling_until(time.time() - 10)  # cooldown lapsed
        assert subs.pool_has_capacity() is True

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert child in result.auto_unblocked
        assert respawns == [child], "the same task must respawn in the unblock tick"

        resumed = kb.get_task(conn, child)
        assert resumed.status == "running"
        # Assignee and dependency edge preserved through block -> unblock -> respawn.
        assert resumed.assignee == "worker"
        assert _parents_of(conn, child) == [parent]


def test_no_auto_unblock_for_non_marker_capability_block(
    isolated_board, tmp_path, monkeypatch, all_assignees_spawnable
):
    """A capability block WITHOUT the subscriptions marker must be left alone
    even when the pool has capacity — auto-unblock is scoped to the pool."""
    monkeypatch.setattr(subs, "is_logged_in", lambda config_dir: True)
    _seed_cooling_pocket(tmp_path, cooling_until=None)  # fully available

    with kb.connect() as conn:
        task = kb.create_task(conn, title="needs a human", assignee="worker")
        assert kb.claim_task(conn, task, claimer="lock-1") is not None
        assert kb.block_task(
            conn, task, reason="missing an API key; human needed", kind="capability"
        ) is True

        assert subs.pool_has_capacity() is True
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        assert task not in result.auto_unblocked
        assert kb.get_task(conn, task).status == "blocked"
