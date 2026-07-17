"""Run-diagnostics visibility: after a worker dies (any way), the card must
show who / where / on what it ran and why it died — without opening the board
file log. Regression cover for the 2026-07-17 incident where workers silently
died on a Codex 429 and the card only said 'gave_up: protocol violation'.
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


def _exited_status(code: int) -> int:
    return code << 8


QUOTA_LOG = (
    "Query: work kanban task t_x\n"
    "Codex provider quota exhausted (429); retry after 505779s. "
    "Credentials are still valid.\n"
    "Goodbye!\n"
)


def _configure_worker(conn, tid, workspace):
    conn.execute(
        "UPDATE tasks SET executor='codex', model_override='claude-opus-4-8', "
        "workspace_path=? WHERE id=?",
        (workspace, tid),
    )
    conn.commit()


def test_run_start_diagnostics_stamped_at_spawn(kanban_home):
    """The who/where/on-what is stamped onto the run at spawn, so it survives a
    worker that dies before ever reporting."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="diag", assignee="a")
        _configure_worker(conn, tid, "/ws/t_x")
        kb.claim_task(conn, tid, claimer="host:w0")
        task = kb.get_task(conn, tid)
        kb._stamp_run_start(conn, task, "/ws/t_x", None)

        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=?", (tid,),
        ).fetchone()
        import json
        meta = json.loads(run["metadata"])
        assert meta["executor"] == "codex"
        assert meta["provider"] == "acp-codex"
        assert meta["model"] == "claude-opus-4-8"
        assert meta["workspace"] == "/ws/t_x"


def test_quota_death_gives_human_reason_and_gave_up_comment(
    kanban_home, monkeypatch,
):
    """Kill a worker on a Codex 429: the parked card must read a human reason
    (not 'exited with code 1'), keep the run's who/where/on-what, and carry a
    gave_up comment with the worker-log tail."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="dies", assignee="a")
        workspace = str(kanban_home / "ws" / tid)
        _configure_worker(conn, tid, workspace)

        # Write the worker log the dispatcher will tail on death.
        log_path = kb.worker_log_path(tid, board=None)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(QUOTA_LOG, encoding="utf-8")

        # DEFAULT_FAILURE_LIMIT is 2: two crashes trip the breaker → gave_up.
        for i in range(2):
            pid = 90000 + i
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid),
            )
            conn.commit()
            task = kb.get_task(conn, tid)
            kb._stamp_run_start(conn, task, workspace, None)
            kb._record_worker_exit(pid, _exited_status(1))  # nonzero → crash
            kb.detect_crashed_workers(conn, board=None)

        task = kb.get_task(conn, tid)
        # (3) circuit breaker parks with a HUMAN reason, not "exited with code 1".
        assert task.status == "blocked"
        assert task.last_failure_error == "Codex quota exhausted (429), retry in 6d"

        # (1) latest run keeps who/where/on-what AND the human reason.
        import json
        run = conn.execute(
            "SELECT outcome, error, metadata FROM task_runs "
            "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        meta = json.loads(run["metadata"])
        assert meta["executor"] == "codex"
        assert meta["provider"] == "acp-codex"
        assert meta["model"] == "claude-opus-4-8"
        assert meta["workspace"] == workspace
        assert "Codex quota exhausted" in (run["error"] or "")

        # (1) gave_up comment carries the diagnostics + worker-log tail.
        comments = kb.list_comments(conn, tid)
        diag = [c for c in comments if c.author == "dispatcher"]
        assert diag, "expected a dispatcher diagnostics comment on gave_up"
        body = diag[-1].body
        assert "Codex quota exhausted (429), retry in 6d" in body
        assert "acp-codex" in body
        assert workspace in body
        assert "retry after 505779s" in body  # the actual log tail


def test_last_run_map_surfaces_diagnostics(kanban_home, monkeypatch):
    """The plugin-side query shape the Zeus card reads: latest run per task with
    outcome + reason + who/where/on-what."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")
        workspace = str(kanban_home / "ws" / tid)
        _configure_worker(conn, tid, workspace)
        log_path = kb.worker_log_path(tid, board=None)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(QUOTA_LOG, encoding="utf-8")

        pid = 91000
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        task = kb.get_task(conn, tid)
        kb._stamp_run_start(conn, task, workspace, None)
        kb._record_worker_exit(pid, _exited_status(1))
        kb.detect_crashed_workers(conn, board=None)

        # Mirror plugin_api._last_run_map's query.
        import json
        rows = conn.execute(
            "SELECT task_id, outcome, error, metadata FROM task_runs WHERE id IN "
            "(SELECT MAX(id) FROM task_runs GROUP BY task_id)"
        ).fetchall()
        by_task = {r["task_id"]: r for r in rows}
        r = by_task[tid]
        meta = json.loads(r["metadata"])
        assert r["outcome"] == "crashed"
        assert "Codex quota exhausted" in (r["error"] or "")
        assert meta["provider"] == "acp-codex"
        assert meta["model"] == "claude-opus-4-8"
