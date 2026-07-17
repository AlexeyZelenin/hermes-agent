"""Tests for autonomous queue replenishment (t_7910a22a).

Covers ``replenish_ready_slots`` (the bounded, rank-ordered, opt-in
todo->ready promoter that keeps execution slots full up to the board
``agent_limit``), the ``auto_replenish`` opt-in flag, the way it composes
with ``recompute_ready``'s dependency gate, and the F9 requirement that the
respawn guard treats the ``promoted_auto`` event like ``promoted`` /
``promoted_manual``.

Every test runs on an isolated board under an isolated HERMES_HOME (via the
``kanban_home`` fixture) — never the live kanban board.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_db)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_meta(board: str, **fields) -> None:
    """Merge ``fields`` into ``board.json`` for ``board`` (test helper).

    ``write_board_metadata`` has no ``auto_replenish`` keyword, so we edit
    the JSON directly — ``read_board_metadata`` preserves unknown keys.
    """
    meta = kb.read_board_metadata(board)
    meta.pop("db_path", None)
    meta.update(fields)
    p = kb.board_metadata_path(board)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def auto_board(kanban_home, monkeypatch):
    """An isolated board opted into ``auto_replenish`` with a small limit.

    Pins ``HERMES_KANBAN_BOARD`` so current-board resolution (used by
    ``recompute_ready``/``_auto_replenish_enabled`` when no explicit board is
    passed) points at this board, mirroring a real worker process.
    """
    slug = "autob"
    kb.create_board(slug, agent_limit=3)
    _set_meta(slug, auto_replenish=True)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", slug)
    return slug


def _force_todo(conn, task_id: str) -> None:
    """Drop a task to ``todo`` regardless of how create_task birthed it."""
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task_id,))
    conn.commit()


def _kinds(conn, task_id: str) -> list[str]:
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


# ---------------------------------------------------------------------------
# _auto_replenish_enabled
# ---------------------------------------------------------------------------

def test_flag_off_by_default(kanban_home):
    kb.create_board("plain")
    assert kb._auto_replenish_enabled("plain") is False


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        ("on", True),
        ("true", True),
        (1, True),
        (False, False),
        ("off", False),
        ("no", False),
        (0, False),
    ],
)
def test_flag_parsing(kanban_home, value, expected):
    kb.create_board("cfg")
    _set_meta("cfg", auto_replenish=value)
    assert kb._auto_replenish_enabled("cfg") is expected


def test_flag_missing_board_is_false(kanban_home):
    # Never raises, defaults False for a board with no metadata file.
    assert kb._auto_replenish_enabled("does-not-exist") is False


# ---------------------------------------------------------------------------
# replenish_ready_slots — happy path
# ---------------------------------------------------------------------------

def test_promotes_assigned_todo_up_to_limit_by_rank(auto_board):
    with kb.connect(board=auto_board) as conn:
        # agent_limit=3, nothing running/ready. Five assigned todo tasks with
        # distinct priorities; rank = priority DESC, created_at ASC.
        ids = {}
        for name, prio in [("a", 1), ("b", 5), ("c", 3), ("d", 2), ("e", 4)]:
            tid = kb.create_task(conn, title=name, assignee="alice", priority=prio)
            _force_todo(conn, tid)
            ids[name] = tid

        promoted = kb.replenish_ready_slots(conn, board=auto_board)

        # Only three slots -> the three highest-priority tasks (b=5, e=4, c=3).
        assert promoted == [ids["b"], ids["e"], ids["c"]]
        for name in ("b", "e", "c"):
            assert kb.get_task(conn, ids[name]).status == "ready"
            assert "promoted_auto" in _kinds(conn, ids[name])
        for name in ("a", "d"):
            assert kb.get_task(conn, ids[name]).status == "todo"


def test_promotion_event_payload(auto_board):
    with kb.connect(board=auto_board) as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)
        kb.replenish_ready_slots(conn, board=auto_board)
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='promoted_auto'",
            (tid,),
        ).fetchone()
        payload = json.loads(ev["payload"])
        assert payload["reason"] == "auto_replenish"
        assert "rank_source" in payload


def test_dry_run_reports_without_mutating(auto_board):
    with kb.connect(board=auto_board) as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)
        promoted = kb.replenish_ready_slots(conn, board=auto_board, dry_run=True)
        assert promoted == [tid]
        # No mutation, no event.
        assert kb.get_task(conn, tid).status == "todo"
        assert "promoted_auto" not in _kinds(conn, tid)


# ---------------------------------------------------------------------------
# replenish_ready_slots — do-not-promote cases
# ---------------------------------------------------------------------------

def test_flag_off_does_not_promote(kanban_home):
    kb.create_board("plain", agent_limit=3)
    with kb.connect(board="plain") as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)
        assert kb.replenish_ready_slots(conn, board="plain") == []
        assert kb.get_task(conn, tid).status == "todo"


def test_unsatisfied_deps_not_promoted(auto_board):
    with kb.connect(board=auto_board) as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent]
        )
        # Parent still running -> child is dependency-gated in todo.
        assert kb.get_task(conn, child).status == "todo"
        promoted = kb.replenish_ready_slots(conn, board=auto_board)
        assert child not in promoted
        assert kb.get_task(conn, child).status == "todo"


def test_limit_reached_promotes_nothing(auto_board):
    with kb.connect(board=auto_board) as conn:
        # agent_limit=3. Fill with 2 running + 1 ready = occupancy 3.
        r1 = kb.create_task(conn, title="r1", assignee="alice")
        r2 = kb.create_task(conn, title="r2", assignee="alice")
        kb.claim_task(conn, r1)
        kb.claim_task(conn, r2)
        kb.create_task(conn, title="ready1", assignee="alice")  # born ready
        waiting = kb.create_task(conn, title="w", assignee="alice")
        _force_todo(conn, waiting)

        assert kb.replenish_ready_slots(conn, board=auto_board) == []
        assert kb.get_task(conn, waiting).status == "todo"


def test_partial_slots_only_fills_remaining(auto_board):
    with kb.connect(board=auto_board) as conn:
        # agent_limit=3, one already running -> exactly two slots free.
        running = kb.create_task(conn, title="run", assignee="alice")
        kb.claim_task(conn, running)
        ids = []
        for i in range(4):
            tid = kb.create_task(conn, title=f"t{i}", assignee="alice", priority=i)
            _force_todo(conn, tid)
            ids.append(tid)
        promoted = kb.replenish_ready_slots(conn, board=auto_board)
        assert len(promoted) == 2


def test_unassigned_todo_not_promoted(auto_board):
    with kb.connect(board=auto_board) as conn:
        tid = kb.create_task(conn, title="orphan")  # no assignee
        _force_todo(conn, tid)
        assert kb.replenish_ready_slots(conn, board=auto_board) == []
        assert kb.get_task(conn, tid).status == "todo"


def test_circuit_broken_todo_not_promoted(auto_board):
    with kb.connect(board=auto_board) as conn:
        tid = kb.create_task(conn, title="broken", assignee="alice", max_retries=1)
        conn.execute(
            "UPDATE tasks SET status='todo', consecutive_failures=5 WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kb.replenish_ready_slots(conn, board=auto_board) == []
        assert kb.get_task(conn, tid).status == "todo"


# ---------------------------------------------------------------------------
# composition with recompute_ready (suppression of the blanket promotion)
# ---------------------------------------------------------------------------

def test_recompute_ready_suppresses_todo_but_keeps_blocked_recovery(auto_board):
    with kb.connect(board=auto_board) as conn:
        # A plain todo with satisfied deps: must NOT be blanket-promoted on an
        # auto_replenish board (the bounded replenisher owns it).
        todo = kb.create_task(conn, title="todo", assignee="alice")
        _force_todo(conn, todo)

        # A blocked task with a done parent: dependency recovery must STILL run.
        parent = kb.create_task(conn, title="parent", assignee="alice")
        blocked = kb.create_task(
            conn, title="blocked", assignee="alice", parents=[parent]
        )
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (blocked,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn, board=auto_board)

        assert kb.get_task(conn, todo).status == "todo"       # suppressed
        assert kb.get_task(conn, blocked).status == "ready"   # recovered
        assert promoted == 1


def test_recompute_ready_blanket_promotes_when_flag_off(kanban_home):
    kb.create_board("plain")
    with kb.connect(board="plain") as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)
        promoted = kb.recompute_ready(conn, board="plain")
        assert promoted == 1
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# dispatch integration
# ---------------------------------------------------------------------------

def test_dispatch_replenishes_and_spawns(auto_board, all_assignees_spawnable):
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect(board=auto_board) as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, board=auto_board)

        # Promoted this tick AND spawned in the same tick (order matters).
        assert tid in res.promoted_auto
        assert tid in spawns
        assert kb.get_task(conn, tid).status == "running"
        assert "promoted_auto" in _kinds(conn, tid)


def test_dispatch_no_replenish_when_flag_off(kanban_home, all_assignees_spawnable):
    kb.create_board("plain", agent_limit=3)
    with kb.connect(board="plain") as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _force_todo(conn, tid)
        res = kb.dispatch_once(conn, spawn_fn=lambda *a: None, board="plain")
        assert res.promoted_auto == []
        # Legacy blanket recompute_ready still promoted it to ready.
        assert "promoted_auto" not in _kinds(conn, tid)


# ---------------------------------------------------------------------------
# F9 — respawn guard must honour promoted_auto like promoted / promoted_manual
# ---------------------------------------------------------------------------

def test_respawn_guard_recent_success_bypassed_by_promoted_auto(kanban_home):
    """A promoted_auto event after a recent success is a deliberate re-queue;
    the guard must release the task, or the autonomous replenisher would
    silently park a just-completed task past the success window (F9)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="rerun-me-auto", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        assert kb.check_respawn_guard(conn, t) == "recent_success"
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'promoted_auto', ?)",
            (t, now - 10),
        )
        assert kb.check_respawn_guard(conn, t) is None
