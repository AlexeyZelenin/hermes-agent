"""Shared fixtures: isolated temp DBs and a Config pinned entirely to tmp_path.

Every test builds its own throwaway sqlite files under a pytest tmp dir — the
watchdog never touches the live ~/.hermes kanban/zeus databases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zeus_watchdog.config import Config

_KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    assignee TEXT,
    status TEXT NOT NULL,
    started_at INTEGER,
    last_heartbeat_at INTEGER
);
"""

_ZEUS_SCHEMA = """
CREATE TABLE claude_subscriptions (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    cooling_until REAL,
    last_limited_at REAL
);
CREATE TABLE subscription_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subscription TEXT NOT NULL,
    kind TEXT NOT NULL,
    reset_at REAL
);
"""


def _init(db: Path, schema: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def kanban_db(tmp_path: Path) -> Path:
    db = tmp_path / "kanban.db"
    _init(db, _KANBAN_SCHEMA)
    return db


@pytest.fixture
def zeus_db(tmp_path: Path) -> Path:
    db = tmp_path / "zeus.db"
    _init(db, _ZEUS_SCHEMA)
    return db


@pytest.fixture
def cfg(tmp_path: Path, kanban_db: Path, zeus_db: Path) -> Config:
    home = tmp_path
    (home / "logs").mkdir(exist_ok=True)
    return Config(
        home=home,
        gateway_pid_file=home / "gateway.pid",
        gateway_log=home / "logs" / "gateway.log",
        kanban_db=kanban_db,
        zeus_db=zeus_db,
        chat_id_file=home / "telegram_chat_id",
        state_file=home / "watchdog.state.json",
        bot_token_env_file=home / ".env",
        bot_token="TESTTOKEN",
        chat_id="999",
    )
