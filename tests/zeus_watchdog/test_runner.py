from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zeus_watchdog.config import Config
from zeus_watchdog.runner import run_once
from zeus_watchdog.state import PROBLEM, RECOVERY, load_state


def _dead_pid_file(cfg: Config):
    cfg.gateway_pid_file.write_text(json.dumps({"pid": 2_000_000_000}), encoding="utf-8")


def _running_task(cfg: Config, tid: str, heartbeat: float):
    conn = sqlite3.connect(cfg.kanban_db)
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,last_heartbeat_at)"
        " VALUES (?,?,?, 'running', ?)",
        (tid, tid, "default", heartbeat),
    )
    conn.commit()
    conn.close()


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, token, chat_id, text):
        self.msgs.append(text)
        return True


def test_run_once_alerts_on_dead_gateway_and_persists(cfg: Config):
    _dead_pid_file(cfg)
    cfg.gateway_log.write_text("boot\n", encoding="utf-8")
    sender = Collector()

    result = run_once(cfg, now=10_000.0, sender=sender)

    assert "gateway_dead" in result.conditions
    assert result.delivered == 1
    assert any("нужно внимание" in m for m in sender.msgs)
    saved = load_state(cfg.state_file)
    assert saved["gateway_dead"]["alerted"] is True


def test_run_once_dry_run_sends_nothing_and_skips_state(cfg: Config):
    _dead_pid_file(cfg)
    sender = Collector()

    result = run_once(cfg, now=10_000.0, sender=sender, dry_run=True)

    assert "gateway_dead" in result.conditions
    assert sender.msgs == []
    assert not cfg.state_file.exists()  # dry-run never persists


def test_run_once_recovery_cycle(cfg: Config):
    # Pass 1: gateway dead -> problem alert.
    _dead_pid_file(cfg)
    sender = Collector()
    run_once(cfg, now=0.0, sender=sender)
    assert any("нужно внимание" in m for m in sender.msgs)

    # Pass 2: gateway back (point pid file at ourselves) -> recovery alert.
    import os

    cfg.gateway_pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    cfg.gateway_log.write_text("tick\n", encoding="utf-8")
    sender2 = Collector()
    result = run_once(cfg, now=50.0, sender=sender2)

    assert result.conditions == []
    assert any("ожил" in m for m in sender2.msgs)
    assert "gateway_dead" not in load_state(cfg.state_file)


def test_run_once_heartbeat_stale_end_to_end(cfg: Config):
    import os

    # Live gateway so only the heartbeat check fires.
    cfg.gateway_pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    cfg.gateway_log.write_text("tick\n", encoding="utf-8")
    _running_task(cfg, "t_stuck", heartbeat=100.0)
    sender = Collector()

    result = run_once(cfg, now=10_000.0, sender=sender)  # 10000s > 1800s timeout

    assert "heartbeat_stale:t_stuck" in result.conditions
    assert any("t_stuck" in m for m in sender.msgs)
