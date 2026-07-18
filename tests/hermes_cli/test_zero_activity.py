"""Zero-activity watchdog + provider-health dispatch gate (task t_08676525)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import provider_health as ph


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, *, executor="claude-code"):
    """Create + claim a task and put a live-looking worker pid on it."""
    t = kb.create_task(conn, title="wedged", assignee="worker", executor=executor)
    kb.claim_task(conn, t)
    kb._set_worker_pid(conn, t, os.getpid())
    return t


def _seed_probe(conn, tid, *, probe_bytes, probe_at, heartbeat_at):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET activity_probe_bytes = ?, activity_probe_at = ?, "
            "last_heartbeat_at = ? WHERE id = ?",
            (probe_bytes, probe_at, heartbeat_at, tid),
        )


def _patch_terminate(monkeypatch, *, terminated=True):
    import hermes_cli.kanban_db as _kb
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: not terminated)
    monkeypatch.setattr(
        _kb, "_terminate_reclaimed_worker",
        lambda *a, **k: {
            "termination_attempted": True,
            "host_local": True,
            "terminated": terminated,
        },
    )


def _probe(conn, tid):
    row = conn.execute(
        "SELECT activity_probe_bytes, activity_probe_at FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()
    return row["activity_probe_bytes"], row["activity_probe_at"]


def _events(conn, tid):
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
        ).fetchall()
    ]


def test_disabled_when_timeout_zero(kanban_home):
    with kb.connect() as conn:
        t = _running_task(conn)
        assert kb.detect_zero_activity(conn, zero_activity_timeout_seconds=0) == []
        assert kb.get_task(conn, t).status == "running"


def test_wedged_worker_reclaimed_and_provider_blacklisted(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=True)
    with kb.connect() as conn:
        t = _running_task(conn, executor="claude-code")
        now = int(time.time())
        # Probe seeded 1000s ago, static log (no file → 0 bytes), stale heartbeat.
        _seed_probe(conn, t, probe_bytes=0, probe_at=now - 1000, heartbeat_at=now - 1000)

        reclaimed = kb.detect_zero_activity(
            conn, zero_activity_timeout_seconds=600, signal_fn=lambda p, s: None,
        )
        assert reclaimed == [t]
        assert kb.get_task(conn, t).status == "ready"
        assert "zero_activity" in _events(conn, t)
        # The ACP provider blamed for the hang is now blacklisted.
        assert ph.is_available(conn, "acp-claude-code") is False


def test_fresh_heartbeat_not_reclaimed(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=True)
    with kb.connect() as conn:
        t = _running_task(conn)
        now = int(time.time())
        # Idle clock is old, but a heartbeat landed within the window → active.
        _seed_probe(conn, t, probe_bytes=0, probe_at=now - 1000, heartbeat_at=now - 10)

        assert kb.detect_zero_activity(conn, zero_activity_timeout_seconds=600) == []
        assert kb.get_task(conn, t).status == "running"
        # The probe clock was reset to "now" on the observed activity.
        _, probe_at = _probe(conn, t)
        assert probe_at >= now


def test_log_growth_grants_reprieve(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=True)
    with kb.connect() as conn:
        t = _running_task(conn)
        now = int(time.time())
        _seed_probe(conn, t, probe_bytes=0, probe_at=now - 1000, heartbeat_at=now - 1000)
        # Heartbeat is stale, but the worker log GREW since the last probe.
        log = kb.worker_log_path(t)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("progress output\n" * 50, encoding="utf-8")

        assert kb.detect_zero_activity(conn, zero_activity_timeout_seconds=600) == []
        assert kb.get_task(conn, t).status == "running"
        probe_bytes, _ = _probe(conn, t)
        assert probe_bytes == log.stat().st_size


def test_unseeded_row_is_seeded_not_reclaimed(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=True)
    with kb.connect() as conn:
        t = _running_task(conn)
        now = int(time.time())
        # No probe yet (legacy / pre-spawn row) but a very stale heartbeat.
        _seed_probe(conn, t, probe_bytes=None, probe_at=None, heartbeat_at=now - 9999)

        assert kb.detect_zero_activity(conn, zero_activity_timeout_seconds=600) == []
        assert kb.get_task(conn, t).status == "running"
        _, probe_at = _probe(conn, t)
        assert probe_at is not None  # seeded this tick


def test_native_worker_not_provider_blacklisted(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=True)
    with kb.connect() as conn:
        t = _running_task(conn, executor="hermes-worker")
        now = int(time.time())
        _seed_probe(conn, t, probe_bytes=0, probe_at=now - 1000, heartbeat_at=now - 1000)

        reclaimed = kb.detect_zero_activity(conn, zero_activity_timeout_seconds=600)
        assert reclaimed == [t]  # still reclaimed — a hang is a hang
        assert kb.get_task(conn, t).status == "ready"
        # But hermes-worker has no external ACP provider to blacklist.
        assert kb.provider_for_task(kb.get_task(conn, t)) is None


def test_defers_when_live_worker_survives(kanban_home, monkeypatch):
    _patch_terminate(monkeypatch, terminated=False)  # worker survives the kill
    with kb.connect() as conn:
        t = _running_task(conn)
        now = int(time.time())
        _seed_probe(conn, t, probe_bytes=0, probe_at=now - 1000, heartbeat_at=now - 1000)

        assert kb.detect_zero_activity(conn, zero_activity_timeout_seconds=600) == []
        assert kb.get_task(conn, t).status == "running"
        assert "reclaim_deferred" in _events(conn, t)


# --- Preflight provider gate (check_respawn_guard, task t_08676525) ----------


def test_gate_defers_ready_task_on_unhealthy_provider(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="worker", executor="claude-code")
        ph.record_unavailable(conn, "acp-claude-code", reason="403", ttl_seconds=600)
        assert kb.check_respawn_guard(conn, t) == "provider_unhealthy"


def test_gate_defers_on_paused_provider(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="worker", executor="codex")
        ph.pause(conn, "acp-codex", by="op", reason="broken")
        assert kb.check_respawn_guard(conn, t) == "provider_paused"


def test_gate_allows_when_provider_healthy(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="worker", executor="claude-code")
        assert kb.check_respawn_guard(conn, t) is None


def test_gate_ignores_native_worker(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="worker", executor="hermes-worker")
        # Even with a same-named blacklist entry, a native worker isn't gated.
        ph.pause(conn, "acp-claude-code", by="op", reason="broken")
        assert kb.check_respawn_guard(conn, t) is None
