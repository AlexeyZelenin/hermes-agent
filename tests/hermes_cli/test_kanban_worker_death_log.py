"""Worker-death diagnostics: when a worker dies, its point-of-breakage must be
captured structurally in BOTH the engine-room log (t_adf37522) and task_events —
exit code, stderr tail, last tool_call/step, timestamp, subscription — so
'почему сломался' is always searchable (feeds the log-watcher + watchdog).

Task t_526a0463.
"""
import json
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


def _exited_status(code: int) -> int:
    return code << 8


CRASH_LOG = (
    "Query: work kanban task t_x\n"
    "Reading file foo.py ...\n"
    "Traceback (most recent call last):\n"
    "RuntimeError: boom\n"
)


def _claim_running(conn, tid, workspace, pid, worker="w0"):
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:{worker}")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    task = kb.get_task(conn, tid)
    kb._stamp_run_start(conn, task, workspace, None)
    return task


def _latest_event(conn, tid, kind):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1",
        (tid, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else None


def test_stamp_run_metadata_records_subscription(kanban_home):
    """The subscription lease is stamped onto the run mid-session, so it's
    readable back after the worker dies (before any clean completion)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="sub", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w0")

    kb.stamp_run_metadata(tid, {"subscription": "claude-acct-3"}, board=None)

    with kb.connect() as conn:
        assert kb._run_subscription(conn, tid) == "claude-acct-3"


def test_crash_writes_engine_log_and_enriches_event(kanban_home, monkeypatch):
    """A nonzero-exit crash: engine-room log gets a structured ``worker_crashed``
    operator row (exit_code + stderr tail + last step + subscription), and the
    live ``crashed`` task_event carries the same breadcrumbs."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dies", assignee="a")
        workspace = str(kanban_home / "ws" / tid)
        conn.execute("UPDATE tasks SET executor='claude-code' WHERE id=?", (tid,))
        conn.commit()

        log_path = kb.worker_log_path(tid, board=None)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(CRASH_LOG, encoding="utf-8")

        pid = 90001
        _claim_running(conn, tid, workspace, pid)
        # Mid-session facts a dying worker leaves behind.
        kb.append_task_event(
            tid, "tool_call",
            {"tool_call_id": "tc1", "status": "running",
             "title": "Read", "input": "foo.py"},
            board=None,
        )
        kb.stamp_run_metadata(tid, {"subscription": "claude-acct-1"}, board=None)

        kb._record_worker_exit(pid, _exited_status(1))
        kb.detect_crashed_workers(conn, board=None)

        # (a) task_events: the crashed event carries the breadcrumbs.
        ev = _latest_event(conn, tid, "crashed")
        assert ev["exit_code"] == 1
        assert ev["exit_kind"] == "nonzero_exit"
        assert ev["subscription"] == "claude-acct-1"
        assert "Read" in ev["last_step"]
        assert "RuntimeError: boom" in ev["stderr_tail"]

        # (b) engine-room log: one structured operator row, searchable.
        logs = kb.query_log(conn, source="operator", event="worker_crashed")
        assert len(logs) == 1
        entry = logs[0]
        assert entry.severity == "error"
        assert entry.category == "worker_death"
        assert entry.task_id == tid
        assert entry.created_at > 0  # timestamp always present
        p = entry.payload
        assert p["exit_code"] == 1
        assert p["exit_kind"] == "nonzero_exit"
        assert p["executor"] == "claude-code"
        assert p["subscription"] == "claude-acct-1"
        assert "Read" in p["last_step"]
        assert "RuntimeError: boom" in p["stderr_tail"]


def test_rate_limited_death_logs_as_warn(kanban_home, monkeypatch):
    """A quota-wall exit is a self-healing throttle, not a crash: it logs a
    ``worker_rate_limited`` row at ``warn``, not an ``error`` crash."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="throttled", assignee="a")
        workspace = str(kanban_home / "ws" / tid)
        pid = 90002
        _claim_running(conn, tid, workspace, pid)
        kb._record_worker_exit(pid, _exited_status(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
        kb.detect_crashed_workers(conn, board=None)

        assert kb.query_log(conn, source="operator", event="worker_crashed") == []
        rl = kb.query_log(conn, source="operator", event="worker_rate_limited")
        assert len(rl) == 1
        assert rl[0].severity == "warn"
        assert rl[0].payload["exit_kind"] == "rate_limited"
