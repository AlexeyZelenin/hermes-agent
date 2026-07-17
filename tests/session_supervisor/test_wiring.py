"""Wiring tests: snapshots from the real kanban DB + zeus ledger, real prober, service tick.

Everything runs against an isolated board (temp ``HERMES_KANBAN_HOME``) and a temp zeus.db, so
the live kanban board and real token ledger are never touched. Skipped when the hermes-agent
repo is not importable.
"""

import os
import socket
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from session_supervisor import Supervisor, SupervisorConfig
from hermes_supervisor import (
    build_run_snapshots,
    count_running,
    token_usage_totals,
)
from hermes_supervisor.prober import ProcessLivenessProber
from hermes_supervisor.service import SupervisorService
from tests.session_supervisor.test_escalation import FakeNotifier
from tests.session_supervisor.util import mins

HERMES_REPO = Path(
    os.environ.get("HERMES_AGENT_REPO", "~/.hermes/hermes-agent")
).expanduser()
if str(HERMES_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_REPO))
kdb = pytest.importorskip("hermes_cli.kanban_db")

T0 = 1_700_000_000.0

# Real zeus token_usage schema (mirrors ~/.hermes/zeus/zeus.db).
_ZEUS_SCHEMA = """
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
    subscription TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_usage_task ON token_usage(task_id);
"""


def _make_zeus(path: Path, rows=()) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_ZEUS_SCHEMA)
    for task_id, ts, total in rows:
        conn.execute(
            "INSERT INTO token_usage (ts, task_id, total_tokens) VALUES (?, ?, ?)",
            (ts, task_id, total),
        )
    conn.commit()
    conn.close()
    return path


def _make_running_task(conn, *, now=T0, hb=None, pid=4242, run=True):
    """Insert a running task and (optionally) an active run row via raw SQL.

    Raw inserts (rather than ``create_task``) keep the fixture independent of board.json,
    executor validation, and workspace resolution, and guarantee the write lands on ``conn``.
    """
    host = socket.gethostname()
    task_id = f"t_{pid:06d}"
    started = int(now - mins(10))
    with kdb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, created_at, started_at, "
            "last_heartbeat_at, worker_pid, claim_lock, executor) "
            "VALUES (?, 'Busy worker', 'x', 'running', ?, ?, ?, ?, ?, 'claude-code')",
            (task_id, started, started, hb, pid, f"{host}:{pid}"),
        )
        if run:
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, status, claim_lock, worker_pid, "
                "last_heartbeat_at, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
                (task_id, f"{host}:{pid}", pid, hb, started),
            )
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (cur.lastrowid, task_id),
            )
    return task_id


@pytest.fixture()
def board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    db = tmp_path / "kanban.db"
    return {"db": db, "tmp": tmp_path}


# -- sources ---------------------------------------------------------------


def test_token_usage_totals_sums_and_takes_latest(tmp_path):
    zeus = _make_zeus(
        tmp_path / "zeus.db",
        rows=[("t_a", T0 - 100, 500), ("t_a", T0 - 10, 1500), ("t_b", T0, 99)],
    )
    conn = sqlite3.connect(str(zeus))
    conn.row_factory = sqlite3.Row
    try:
        total, last_ts = token_usage_totals(conn, "t_a")
    finally:
        conn.close()
    assert total == 2000
    assert last_ts == T0 - 10


def test_token_usage_totals_missing_table_is_zero(tmp_path):
    empty = sqlite3.connect(str(tmp_path / "empty.db"))
    empty.row_factory = sqlite3.Row
    try:
        assert token_usage_totals(empty, "t_a") == (0, None)
    finally:
        empty.close()


def test_build_run_snapshots_reads_board_and_ledger(board):
    conn = kdb.connect(board["db"])
    try:
        task_id = _make_running_task(conn, now=T0, hb=None, pid=555)
        zeus = _make_zeus(board["tmp"] / "zeus.db", rows=[(task_id, T0 - 20, 1234)])
        zconn = sqlite3.connect(str(zeus))
        zconn.row_factory = sqlite3.Row
        try:
            snaps = build_run_snapshots(conn, "testboard", zconn, T0)
        finally:
            zconn.close()
    finally:
        conn.close()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.task_id == task_id
    assert snap.status == "running"
    assert snap.tokens_total == 1234
    assert snap.last_token_usage_at == T0 - 20
    assert snap.worker_id == f"{socket.gethostname()}:555"
    assert snap.started_at == T0 - mins(10)


def test_snapshot_drives_stall_detection(board):
    """A real running task with no heartbeat/tokens is detected as R3 stalled over two ticks."""
    conn = kdb.connect(board["db"])
    try:
        _make_running_task(conn, now=T0, hb=None, pid=777)
        zeus = _make_zeus(board["tmp"] / "zeus.db")

        def read_snaps(now):
            zconn = sqlite3.connect(str(zeus))
            zconn.row_factory = sqlite3.Row
            try:
                return build_run_snapshots(conn, "testboard", zconn, now)
            finally:
                zconn.close()

        sup = Supervisor(
            config=SupervisorConfig(),
            prober=lambda run, at: __import__(
                "session_supervisor.snapshots", fromlist=["ProbeResult"]
            ).ProbeResult(False, "dead"),
            state_path=str(board["tmp"] / "sup.json"),
        )
        assert sup.tick(T0, read_snaps(T0)) == []  # suspected
        events = sup.tick(T0 + 60, read_snaps(T0 + 60))  # probe fails -> confirmed
    finally:
        conn.close()
    assert [e["kind"] for e in events] == ["incident_opened"]
    assert events[0]["anomaly_type"] == "stalled"


def test_count_running(board):
    conn = kdb.connect(board["db"])
    try:
        assert count_running(conn) == 0
        _make_running_task(conn, pid=1)
        _make_running_task(conn, pid=2)
        assert count_running(conn) == 2
    finally:
        conn.close()


# -- prober ----------------------------------------------------------------


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code])


def _run_for_pid(pid: int):
    from session_supervisor import RunSnapshot

    return RunSnapshot(
        run_id="r", board="ra", task_id="t", task_title="t", session_id="s",
        worker_id=f"{socket.gethostname()}:{pid}", attempt=1, status="running",
        started_at=T0, last_heartbeat_at=None, last_token_usage_at=None,
        tokens_total=0, token_budget=None, log_ref="",
    )


def test_prober_busy_process_is_alive():
    proc = _spawn("x=0\nwhile True:\n x+=1")
    try:
        prober = ProcessLivenessProber(sample_seconds=0.4)
        result = prober(_run_for_pid(proc.pid), "stalled")
    finally:
        proc.kill()
        proc.wait()
    assert result.alive is True
    assert result.detail.startswith("cpu_active")


def test_prober_idle_process_is_not_alive():
    proc = _spawn("import time; time.sleep(30)")
    time.sleep(0.3)  # let interpreter startup settle so the sample sees a truly idle process
    try:
        prober = ProcessLivenessProber(sample_seconds=0.4)
        result = prober(_run_for_pid(proc.pid), "stalled")
    finally:
        proc.kill()
        proc.wait()
    assert result.alive is False
    assert result.detail.startswith("no_cpu_progress")


def test_prober_dead_process_is_not_alive():
    proc = _spawn("import time; time.sleep(30)")
    pid = proc.pid
    proc.kill()
    proc.wait()
    time.sleep(0.1)
    result = ProcessLivenessProber(sample_seconds=0.1)(_run_for_pid(pid), "stalled")
    assert result.alive is False
    assert result.detail in ("process_gone", "zombie")


def test_prober_remote_host_is_not_alive():
    from session_supervisor import RunSnapshot

    run = RunSnapshot(
        run_id="r", board="ra", task_id="t", task_title="t", session_id="s",
        worker_id="some-other-host:999", attempt=1, status="running",
        started_at=T0, last_heartbeat_at=None, last_token_usage_at=None,
        tokens_total=0, token_budget=None, log_ref="",
    )
    result = ProcessLivenessProber(sample_seconds=0.1)(run, "stalled")
    assert result.alive is False
    assert result.detail.startswith("remote_host_unprobed")


# -- service (end to end) --------------------------------------------------


def _service(board, notifier, **kw):
    return SupervisorService(
        "testboard",
        state_dir=str(board["tmp"] / "state"),
        board_meta={"icon": "⚡", "agent_limit": 3, "supervisor": {}},
        hermes_repo=str(HERMES_REPO),
        db_path=str(board["db"]),
        zeus_db_path=str(board["tmp"] / "zeus.db"),
        notifier=notifier,
        prober=lambda run, at: __import__(
            "session_supervisor.snapshots", fromlist=["ProbeResult"]
        ).ProbeResult(False, "dead"),
        **kw,
    )


def _incident_cards(db):
    conn = kdb.connect(Path(db))
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tasks WHERE title LIKE '[incident]%' ORDER BY created_at"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_service_stall_produces_one_card_and_one_push(board):
    _make_zeus(board["tmp"] / "zeus.db")
    conn = kdb.connect(board["db"])
    try:
        _make_running_task(conn, now=T0, hb=None, pid=888)
    finally:
        conn.close()
    notifier = FakeNotifier()
    svc = _service(board, notifier)

    svc.run_tick(T0, ready_nonempty=False, spawned=True, free_slots=3)  # suspected
    assert _incident_cards(str(board["db"])) == []
    summary = svc.run_tick(T0 + 60, ready_nonempty=False, spawned=True, free_slots=3)

    cards = _incident_cards(str(board["db"]))
    assert len(cards) == 1
    assert cards[0]["status"] == "triage"
    assert cards[0]["title"].startswith("[incident] stalled:")
    assert len(notifier.sent) == 1
    assert summary["events"] == 1


def test_service_dispatcher_stall_escalates_after_three_ticks(board):
    _make_zeus(board["tmp"] / "zeus.db")
    notifier = FakeNotifier()
    svc = _service(board, notifier)
    for i in range(3):
        svc.run_tick(T0 + i * 60, ready_nonempty=True, spawned=False, free_slots=2)
    cards = _incident_cards(str(board["db"]))
    assert len(cards) == 1
    assert cards[0]["title"].startswith("[incident] dispatcher_stall:")
    assert len(notifier.sent) == 1


def test_service_tick_never_raises_on_bad_board(board, monkeypatch):
    svc = _service(board, FakeNotifier())
    monkeypatch.setattr(svc, "_connect_board", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc.run_tick(T0, ready_nonempty=False, spawned=True) == {"error": True}
