from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from zeus_watchdog import checks


def _add_task(db: Path, tid, status, started_at=None, heartbeat=None, assignee=""):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,started_at,last_heartbeat_at)"
        " VALUES (?,?,?,?,?,?)",
        (tid, tid, assignee, status, started_at, heartbeat),
    )
    conn.commit()
    conn.close()


def _add_sub(db: Path, name, cooling_until=None, last_limited_at=None, enabled=1):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO claude_subscriptions (name,enabled,cooling_until,last_limited_at)"
        " VALUES (?,?,?,?)",
        (name, enabled, cooling_until, last_limited_at),
    )
    conn.commit()
    conn.close()


def _add_limit_event(db: Path, name, ts, reset_at):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO subscription_events (ts,subscription,kind,reset_at)"
        " VALUES (?,?, 'limit', ?)",
        (ts, name, reset_at),
    )
    conn.commit()
    conn.close()


# --- IO probes --------------------------------------------------------------

def test_read_gateway_pid_json_and_bare(tmp_path: Path):
    p = tmp_path / "gateway.pid"
    p.write_text(json.dumps({"pid": 4321, "kind": "hermes-gateway"}), encoding="utf-8")
    assert checks.read_gateway_pid(p) == 4321
    p.write_text("777", encoding="utf-8")
    assert checks.read_gateway_pid(p) == 777
    assert checks.read_gateway_pid(tmp_path / "missing") is None


def test_pid_alive_self_and_dead():
    assert checks.pid_alive(os.getpid()) is True
    assert checks.pid_alive(None) is False
    assert checks.pid_alive(2_000_000_000) is False  # implausibly high, not running


def test_query_queue(kanban_db: Path):
    _add_task(kanban_db, "a", "ready", assignee="default")
    _add_task(kanban_db, "b", "ready", assignee="")  # unassigned, excluded
    _add_task(kanban_db, "c", "running")
    ready, run = checks.query_queue(kanban_db)
    assert (ready, run) == (1, 1)


def test_query_queue_missing_db_is_zero(tmp_path: Path):
    assert checks.query_queue(tmp_path / "nope.db") == (0, 0)


def test_stale_heartbeats_uses_heartbeat_then_started(kanban_db: Path):
    now = 10_000.0
    _add_task(kanban_db, "fresh", "running", heartbeat=now - 10)
    _add_task(kanban_db, "stale", "running", heartbeat=now - 5000)
    _add_task(kanban_db, "nostart", "running")  # no beat, no start -> skipped
    _add_task(kanban_db, "onlystart", "running", started_at=now - 5000)
    _add_task(kanban_db, "zerobeat", "running", heartbeat=0.0)  # epoch 0, not "missing"
    out = dict(checks.stale_heartbeats(kanban_db, now, timeout_sec=1800))
    assert set(out) == {"stale", "onlystart", "zerobeat"}


def test_quick_check_ok_and_corrupt(kanban_db: Path, tmp_path: Path):
    assert checks.quick_check(kanban_db) == "ok"
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database at all")
    assert checks.quick_check(bad) not in (None, "ok")
    assert checks.quick_check(tmp_path / "missing.db") is None


def test_pool_recovered_after_limit(zeus_db: Path):
    now = 100_000.0
    # Recent limit, cooldown elapsed -> recovered.
    _add_sub(zeus_db, "work1", cooling_until=now - 100, last_limited_at=now - 500)
    _add_limit_event(zeus_db, "work1", ts=now - 400, reset_at=now - 100)
    assert checks.pool_recovered_after_limit(zeus_db, now, window_sec=6 * 3600) is True


def test_pool_not_recovered_while_cooling(zeus_db: Path):
    now = 100_000.0
    _add_sub(zeus_db, "work1", cooling_until=now + 500, last_limited_at=now - 100)
    _add_limit_event(zeus_db, "work1", ts=now - 100, reset_at=now + 500)
    assert checks.pool_recovered_after_limit(zeus_db, now, window_sec=6 * 3600) is False


def test_pool_recovered_false_without_recent_limit(zeus_db: Path):
    now = 100_000.0
    _add_sub(zeus_db, "work1", cooling_until=now - 100, last_limited_at=now - 99999)
    _add_limit_event(zeus_db, "work1", ts=now - 99999, reset_at=now - 90000)
    assert checks.pool_recovered_after_limit(zeus_db, now, window_sec=3600) is False
