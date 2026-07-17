"""Live tool-call feed persistence for running ACP executors.

The ACP task executor streams tool_call / tool_call_update activity into the
active run's metadata and emits ``tool_call`` events so the dashboard card and
drawer render a live feed of what the executor is doing (including sub-agent
spawns). These cover the DB-side persistence + batch read used by the board.
"""
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (never the live board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn):
    tid = kb.create_task(conn, title="live", assignee="a")
    kb.claim_task(conn, tid, claimer="host:w0")
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, run_id


def test_record_run_tool_activity_persists_snapshot_and_event(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _running_task(conn)
        snapshot = [
            {"id": "tc-1", "seq": 1, "title": "Task: spawn subagent",
             "kind": "other", "status": "in_progress"},
        ]
        ok = kb.record_run_tool_activity(
            conn, tid, run_id, snapshot, changed=snapshot[0],
        )
        assert ok is True

        run = kb.get_run(conn, run_id)
        assert run.metadata["tool_calls"] == snapshot

        events = kb.list_events(conn, tid)
        tool_events = [e for e in events if e.kind == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].run_id == run_id
        assert tool_events[0].payload["status"] == "in_progress"
        assert tool_events[0].payload["title"] == "Task: spawn subagent"
        # The event payload is compact - no content/raw io leaks onto it.
        assert set(tool_events[0].payload) == {"id", "seq", "title", "kind", "status"}


def test_record_run_tool_activity_merges_and_updates(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _running_task(conn)
        # Pre-existing start metadata must survive the tool_calls merge.
        start_meta = kb.get_run(conn, run_id).metadata or {}
        kb.record_run_tool_activity(
            conn, tid, run_id, [{"id": "a", "seq": 1, "status": "pending"}],
        )
        kb.record_run_tool_activity(
            conn, tid, run_id, [{"id": "a", "seq": 1, "status": "completed"}],
        )
        meta = kb.get_run(conn, run_id).metadata
        assert meta["tool_calls"] == [{"id": "a", "seq": 1, "status": "completed"}]
        for key, value in start_meta.items():
            assert meta[key] == value


def test_record_run_tool_activity_no_run_is_noop(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idle", assignee="a")
        assert kb.record_run_tool_activity(conn, tid, None, [{"id": "x"}]) is False


def test_latest_run_tool_calls_reads_active_run(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _running_task(conn)
        other = kb.create_task(conn, title="no-run", assignee="a")
        snapshot = [{"id": "tc-1", "seq": 1, "title": "Grep", "status": "completed"}]
        kb.record_run_tool_activity(conn, tid, run_id, snapshot)

        feed = kb.latest_run_tool_calls(conn, [tid, other])
        assert feed[tid] == snapshot
        # A task with no active run / no activity is simply absent.
        assert other not in feed
        assert kb.latest_run_tool_calls(conn, []) == {}
