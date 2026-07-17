"""HTTP tests for the engine-room log plugin routes (t_adf37522):
POST /client-log (ingest the browser's own log) and GET /engine-log (unified
search). Exercises the full FastAPI path against an isolated board.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_enginelog_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB — never touches real data."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_client_log_ingest_and_query_roundtrip(client):
    """The browser's log batch is sanitised, stored, and searchable back."""
    r = client.post("/api/plugins/kanban/client-log", json={
        "session_id": "web-sess",
        "entries": [
            {"event": "ws.open", "severity": "info", "category": "ws"},
            {"event": "task.move", "severity": "info", "task_id": "t_1",
             "payload": {"to": "running"}},
            {"severity": "info"},  # no event -> dropped by the sanitizer
        ],
    })
    assert r.status_code == 200
    assert r.json() == {"accepted": 2, "received": 3}

    got = client.get("/api/plugins/kanban/engine-log?source=client")
    assert got.status_code == 200
    entries = got.json()["entries"]
    assert {e["event"] for e in entries} == {"ws.open", "task.move"}
    # Source is forced to client; session id back-filled from the batch.
    assert all(e["source"] == "client" for e in entries)
    assert all(e["session_id"] == "web-sess" for e in entries)
    move = next(e for e in entries if e["event"] == "task.move")
    assert move["payload"] == {"to": "running"} and move["task_id"] == "t_1"


def test_client_log_never_rejects_a_malformed_batch(client):
    """Best-effort by design: garbage entries drop, the request still 200s."""
    r = client.post("/api/plugins/kanban/client-log", json={
        "entries": ["garbage", {"no": "event"}, 123],
    })
    assert r.status_code == 200
    assert r.json()["accepted"] == 0


def test_engine_log_severity_floor_and_source_filter(client):
    """GET /engine-log applies a minimum-severity floor and validates source."""
    client.post("/api/plugins/kanban/client-log", json={"entries": [
        {"event": "noise", "severity": "debug"},
        {"event": "boom", "severity": "error", "payload": {"why": "kaboom"}},
    ]})
    # severity is a *minimum*: an error floor keeps only the error line.
    errs = client.get("/api/plugins/kanban/engine-log?severity=error").json()["entries"]
    assert {e["event"] for e in errs} == {"boom"}
    # substring search spans event + payload.
    hits = client.get("/api/plugins/kanban/engine-log?q=kaboom").json()["entries"]
    assert {e["event"] for e in hits} == {"boom"}
    # an unknown source is a clean 400, not a 500.
    bad = client.get("/api/plugins/kanban/engine-log?source=operatorX")
    assert bad.status_code == 400
