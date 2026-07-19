"""Regression tests for t_d5a8eafe — anonymous empty unassigned tasks
(no body, no created_by, no assignee) must never enter a dispatchable lane.

Historical failure: one-word titles like 'ship', 'engine', 'uncat' with
body=NULL, created_by=NULL, AND assignee=NULL reached ``ready`` on the
live board, stalled the queue, and wasted spawn slots. The root fix is a
three-layer gate:

1. ``create_task`` routes such rows to ``triage`` at insertion time
   (the create-gate).
2. ``dispatch_once`` skips any ready row that bypassed the gate (older
   binary, direct SQL, restored backup) — defensive net.
3. ``sweep_empty_tasks`` (run from the dispatch tick) reclassifies
   stray anonymous empty unassigned rows from ``ready``/``todo`` back to
   ``triage`` so the board self-heals.

The three-way NULL check (body + created_by + assignee) is the key
discriminator: every production caller (CLI, dashboard, agent tool)
sets at least one of created_by or assignee. Only rows that bypassed
every entry point — direct SQL, older binary, UI typo — hit all three
NULLs. A task with an assignee but no body is intentional (the caller
meant to create it); a task with nothing at all is junk.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB (never touches the live board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _status(conn, task_id):
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["status"]


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC",
        (task_id, kind),
    ).fetchall()


def _insert_raw_ghost(conn, title="ghost", *, assignee=None, body=None,
                      created_by=None, status="ready"):
    """Bypass create_task to simulate a pre-gate row (direct SQL, restored
    backup, older binary) — exactly what the defensive net and sweep must catch."""
    task_id = kb._new_task_id()
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, "
            "created_by, created_at, workspace_kind, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'scratch', ?)",
            (task_id, title, body, assignee, status, created_by, now, now),
        )
        kb._append_event(conn, task_id, "created", {"status": status})
    return task_id


# ---- create-gate (layer 1) -------------------------------------------------


def test_anonymous_empty_unassigned_task_lands_in_triage(kanban_home):
    """The core fix: a task with no body, no created_by, AND no assignee is
    routed to ``triage`` instead of ``ready``."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship", body=None, created_by=None, assignee=None
        )
        assert _status(conn, tid) == "triage"


def test_create_gate_tags_created_event(kanban_home):
    """The ``created`` event is tagged ``routed_by_create_gate`` so operators
    can see *why* a freshly-created task is parked in triage."""
    import json

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship", body=None, created_by=None, assignee=None
        )
        rows = _events(conn, tid, "created")
        assert rows, "expected a 'created' event"
        payload = json.loads(rows[0]["payload"])
        assert payload.get("routed_by_create_gate") is True


def test_empty_body_with_created_by_is_not_gated(kanban_home):
    """An empty body is fine when the task has an author — dashboards and
    scripts legitimately create title-only cards (the body gets filled in
    later by a specifier)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idea", body=None, created_by="dashboard")
        assert _status(conn, tid) == "ready"


def test_empty_body_with_assignee_is_not_gated(kanban_home):
    """An empty body is fine when the task has an assignee — the caller
    clearly meant to create it and route it. This is the key discriminator
    that lets programmatic callers (tests, internal code) create minimal
    tasks without tripping the gate."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="do thing", body=None, created_by=None, assignee="alice"
        )
        assert _status(conn, tid) == "ready"


def test_body_present_no_created_by_no_assignee_is_not_gated(kanban_home):
    """A task with a body but no created_by and no assignee is also fine —
    the body itself is the spec. The gate is specifically about the
    *combination* of all three being absent."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="do thing", body="the thing, in detail",
            created_by=None, assignee=None,
        )
        assert _status(conn, tid) == "ready"


def test_whitespace_body_treated_as_empty(kanban_home):
    """A whitespace-only body is the same as no body — the gate must strip
    before checking."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship", body="   \n\t ",
            created_by=None, assignee=None,
        )
        assert _status(conn, tid) == "triage"


def test_explicit_triage_is_not_double_marked(kanban_home):
    """An explicit ``triage=True`` already parks the task; the gate must
    not double-mark or change behavior."""
    import json

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="idea", body=None, created_by=None,
            assignee=None, triage=True,
        )
        assert _status(conn, tid) == "triage"
        # Explicit triage is the caller's intent — the gate did not "trip",
        # the caller asked for triage directly.
        rows = _events(conn, tid, "created")
        payload = json.loads(rows[0]["payload"])
        assert payload.get("routed_by_create_gate") is None


def test_initial_status_blocked_is_not_gated(kanban_home):
    """A caller that explicitly sets ``initial_status='blocked'`` is asking
    for human-ops review; the gate must not override that into triage."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ops task",
            body=None,
            created_by=None,
            assignee=None,
            initial_status="blocked",
        )
        assert _status(conn, tid) == "blocked"


def test_anonymous_empty_child_with_owned_parent_is_not_gated(kanban_home):
    """When the parent has an assignee, the child inherits it at create
    time (the existing owner-inheritance invariant from t_0831813e), so
    the child has an assignee and the gate does not trip — even though
    the child's body and created_by are NULL."""
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="parent", body="spec",
            created_by="user", assignee="alice",
        )
        child = kb.create_task(
            conn, title="child", body=None, created_by=None, parents=[parent]
        )
        # Child inherits 'alice' from parent (owner-inheritance invariant),
        # so the gate sees an assignee and does not trip. The child lands
        # in 'todo' (parent not done).
        assert _status(conn, child) == "todo"


# ---- defensive dispatcher net (layer 2) ------------------------------------


def test_dispatch_skips_anonymous_empty_unassigned_ready_task(
    kanban_home, monkeypatch, caplog,
):
    """A ready task with no body, no created_by, AND no assignee must NOT
    be spawned. Layer 2 catches what layer 1 missed.

    The sweep janitor normally heals such rows before the dispatch loop
    sees them, so here we disable the sweep to exercise the defensive net
    in isolation — it's the safety rail for when the sweep is disabled,
    a race inserts a ghost between sweep and SELECT, or a bug in the
    sweep query leaves a row behind."""
    monkeypatch.setattr(kb, "sweep_empty_tasks", lambda conn: 0)
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost")
        assert _status(conn, ghost) == "ready"
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            res = kb.dispatch_once(conn, spawn_fn=lambda *_: 1234, dry_run=False)
    assert ghost in res.skipped_empty_anonymous
    assert ghost not in [s[0] for s in res.spawned]
    assert any(
        "anonymous empty" in rec.getMessage() and ghost in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


def test_dispatch_does_not_skip_task_with_body(kanban_home):
    """Sanity: a normal ready task with a body dispatches."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="real work",
            body="a real spec",
            created_by="dashboard",
            assignee="alice",
        )
        assert _status(conn, tid) == "ready"
        res = kb.dispatch_once(conn, spawn_fn=lambda *_: 1234, dry_run=True)
    assert tid not in res.skipped_empty_anonymous


def test_dispatch_does_not_skip_anonymous_task_with_assignee(kanban_home):
    """The net checks all three NULLs. A task with an assignee but no body
    and no created_by is still dispatchable (the caller set the assignee,
    so it's intentional)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="assigned only",
            body=None,
            created_by=None,
            assignee="alice",
        )
        res = kb.dispatch_once(conn, spawn_fn=lambda *_: 1234, dry_run=True)
    assert tid not in res.skipped_empty_anonymous


# ---- sweep_empty_tasks janitor (layer 3) -----------------------------------


def test_sweep_reclassifies_ghost_ready_to_triage(kanban_home):
    """The janitor moves a stray anonymous empty unassigned ready task to
    triage and emits a ``swept_empty`` event so the reason is visible."""
    import json

    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost")
        assert _status(conn, ghost) == "ready"
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 1
        assert _status(conn, ghost) == "triage"
        rows = _events(conn, ghost, "swept_empty")
        assert rows, "expected a swept_empty event"
        payload = json.loads(rows[0]["payload"])
        assert "create-gate" in payload["reason"]


def test_sweep_reclassifies_ghost_todo_to_triage(kanban_home):
    """The janitor also catches ``todo`` rows — they would otherwise
    auto-promote to ready on the next tick and waste another cycle."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost", status="todo")
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 1
        assert _status(conn, ghost) == "triage"


def test_sweep_is_idempotent(kanban_home):
    """Running sweep twice doesn't double-process — once a task is in
    triage it's out of scope (the operator / specifier owns it from there)."""
    with kb.connect() as conn:
        _insert_raw_ghost(conn, title="ghost")
        first = kb.sweep_empty_tasks(conn)
        second = kb.sweep_empty_tasks(conn)
    assert first == 1
    assert second == 0


def test_sweep_leaves_task_with_body_alone(kanban_home):
    """A ready task with a body is not swept, even if created_by and
    assignee are NULL — the body itself makes it a real task."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost", body="a real spec")
        assert _status(conn, ghost) == "ready"
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 0
        assert _status(conn, ghost) == "ready"


def test_sweep_leaves_task_with_created_by_alone(kanban_home):
    """A ready task with a created_by is not swept even with no body and
    no assignee — the author identifies it as intentional."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost", created_by="dashboard")
        assert _status(conn, ghost) == "ready"
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 0
        assert _status(conn, ghost) == "ready"


def test_sweep_leaves_task_with_assignee_alone(kanban_home):
    """A ready task with an assignee is not swept even with no body and
    no created_by — the assignee identifies it as intentional work."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost", assignee="alice")
        assert _status(conn, ghost) == "ready"
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 0
        assert _status(conn, ghost) == "ready"


def test_sweep_leaves_paused_tasks_alone(kanban_home):
    """A paused task is not touched by the sweep — pause is an explicit
    operator hold, the sweep must not silently change its status."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET paused=1 WHERE id=?", (ghost,))
        swept = kb.sweep_empty_tasks(conn)
        assert swept == 0
        assert _status(conn, ghost) == "ready"


def test_dispatch_tick_runs_sweep(kanban_home):
    """The dispatch tick invokes sweep_empty_tasks — a stray ghost is
    reclassified and surfaced in ``result.swept_empty``."""
    with kb.connect() as conn:
        ghost = _insert_raw_ghost(conn, title="ghost")
        assert _status(conn, ghost) == "ready"
        res = kb.dispatch_once(conn, spawn_fn=lambda *_: 1234, dry_run=False)
        assert res.swept_empty == 1
        assert _status(conn, ghost) == "triage"
