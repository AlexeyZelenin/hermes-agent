"""Tests for the first-class PAUSE flag (hermes_cli.kanban_db).

Pause is a flag, not a status: it preserves the task's underlying status and
tells the dispatcher to skip the task in both promotion (recompute_ready) and
spawn (dispatch tick). Resume clears the flag; the task keeps its place.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Schema / flag basics
# ---------------------------------------------------------------------------

def test_fresh_task_is_not_paused(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        assert kb.get_task(conn, tid).paused is False


def test_pause_sets_flag_and_preserves_status(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship", assignee="alice")
        assert kb.get_task(conn, tid).status == "ready"
        ok, err = kb.pause_task(conn, tid, actor="op", reason="hold it")
        assert (ok, err) == (True, None)
        t = kb.get_task(conn, tid)
        assert t.paused is True
        assert t.status == "ready"  # status preserved, not a new status
        events = [e.kind for e in kb.list_events(conn, tid)]
        assert "paused" in events


def test_resume_clears_flag(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship", assignee="alice")
        kb.pause_task(conn, tid)
        ok, err = kb.resume_task(conn, tid, actor="op")
        assert (ok, err) == (True, None)
        t = kb.get_task(conn, tid)
        assert t.paused is False
        assert t.status == "ready"
        events = [e.kind for e in kb.list_events(conn, tid)]
        assert "resumed" in events


def test_pause_is_idempotent_no_duplicate_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship", assignee="alice")
        kb.pause_task(conn, tid)
        kb.pause_task(conn, tid)  # second call is a no-op
        paused_events = [e for e in kb.list_events(conn, tid) if e.kind == "paused"]
        assert len(paused_events) == 1


def test_resume_when_not_paused_is_noop(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship", assignee="alice")
        ok, err = kb.resume_task(conn, tid)
        assert (ok, err) == (True, None)
        resumed = [e for e in kb.list_events(conn, tid) if e.kind == "resumed"]
        assert resumed == []


def test_pause_rejects_running_task(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship", assignee="alice")
        kb.claim_task(conn, tid)  # ready -> running
        assert kb.get_task(conn, tid).status == "running"
        ok, err = kb.pause_task(conn, tid)
        assert ok is False
        assert "running" in err
        assert kb.get_task(conn, tid).paused is False


def test_pause_missing_task(kanban_home):
    with kb.connect() as conn:
        ok, err = kb.pause_task(conn, "t_ghost")
        assert ok is False
        assert "not found" in err


def test_pause_triage_and_todo_allowed(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        ok, _ = kb.pause_task(conn, child)
        assert ok is True
        assert kb.get_task(conn, child).paused is True


# ---------------------------------------------------------------------------
# Dispatcher eligibility
# ---------------------------------------------------------------------------

def test_recompute_ready_skips_paused_todo(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="bob", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        kb.pause_task(conn, child)
        # Parent finishes -> child would normally promote to ready.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent)
        kb.recompute_ready(conn)
        # Paused child stays in todo (status preserved behind the flag).
        assert kb.get_task(conn, child).status == "todo"
        # Unpause -> next recompute promotes it.
        kb.resume_task(conn, child)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dispatch_skips_paused_ready(kanban_home, all_assignees_spawnable):
    spawned_ids: list[str] = []

    def _spawn(task, workspace):
        spawned_ids.append(task.id)
        return 4242  # pretend pid

    with kb.connect() as conn:
        active = kb.create_task(conn, title="active", assignee="alice")
        held = kb.create_task(conn, title="held", assignee="bob")
        assert kb.get_task(conn, active).status == "ready"
        assert kb.get_task(conn, held).status == "ready"
        kb.pause_task(conn, held)

        kb.dispatch_once(conn, spawn_fn=_spawn)

        assert active in spawned_ids
        assert held not in spawned_ids
        # Paused task is untouched: still ready, still paused, never claimed.
        held_t = kb.get_task(conn, held)
        assert held_t.status == "ready"
        assert held_t.paused is True
        assert held_t.claim_lock is None


def test_dispatch_spawns_after_resume(kanban_home, all_assignees_spawnable):
    spawned_ids: list[str] = []

    def _spawn(task, workspace):
        spawned_ids.append(task.id)
        return 4242

    with kb.connect() as conn:
        held = kb.create_task(conn, title="held", assignee="alice")
        kb.pause_task(conn, held)
        kb.dispatch_once(conn, spawn_fn=_spawn)
        assert held not in spawned_ids

        kb.resume_task(conn, held)
        kb.dispatch_once(conn, spawn_fn=_spawn)
        assert held in spawned_ids


# ---------------------------------------------------------------------------
# CLI verbs
# ---------------------------------------------------------------------------

def _pause_ns(task_id, *, ids=None, reason=None, as_json=False):
    return argparse.Namespace(
        task_id=task_id, ids=list(ids or []) or None,
        reason=list(reason or []), json=as_json,
    )


def _resume_ns(task_id, *, ids=None, as_json=False):
    return argparse.Namespace(
        task_id=task_id, ids=list(ids or []) or None, json=as_json,
    )


def test_cli_pause_and_resume_roundtrip(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
    assert kb_cli._cmd_pause(_pause_ns(tid, reason=["hold"])) == 0
    assert "Paused" in capsys.readouterr().out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).paused is True
    assert kb_cli._cmd_resume(_resume_ns(tid)) == 0
    assert "Resumed" in capsys.readouterr().out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).paused is False


def test_cli_pause_running_exits_1(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kb.claim_task(conn, tid)  # -> running
    rc = kb_cli._cmd_pause(_pause_ns(tid))
    assert rc == 1
    assert "cannot pause" in capsys.readouterr().err


def test_cli_pause_bulk_json_emits_list(kanban_home, capsys):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="alice")
        b = kb.create_task(conn, title="b", assignee="bob")
    rc = kb_cli._cmd_pause(_pause_ns(a, ids=[b], as_json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and {r["task_id"] for r in payload} == {a, b}
    assert all(r["paused"] for r in payload)
