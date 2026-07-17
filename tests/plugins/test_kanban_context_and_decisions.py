"""Structural card fields: task CONTEXT ("why") + RELATED DECISIONS (t_cd5e8574).

Two additions land here:

  * ``tasks.context`` — a free-text background/"why" field kept SEPARATE from
    ``body`` (the work description), so opening a card shows the reader the why
    without digging. Exercised at the DB layer (``kanban_db``) and through the
    dashboard plugin's REST surface (create / patch / get).

  * ``kanban_db.list_task_decisions`` — a DEFENSIVE read of a sibling-owned
    ``decisions`` table (t_6dc73752). It must degrade to ``[]`` when the table
    is absent or lacks a ``task_id`` column, and return card-scoped rows
    newest-first when the table exists. The dashboard surfaces these on the
    card so the reader sees "что нарешали" without digging.

The plugin router is attached to a bare FastAPI app (mirroring
``test_kanban_dashboard_plugin.py``) so the REST surface is testable without
the full dashboard. All state is an isolated per-test ``HERMES_HOME``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/plugins/test_kanban_dashboard_plugin.py)
# ---------------------------------------------------------------------------


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_ctx_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
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


def _make_decisions_table(with_task_id: bool = True) -> None:
    """Create a stand-in ``decisions`` table on the active board's DB.

    Simulates the sibling feature (t_6dc73752) so the defensive reader has a
    real table to scope against. ``with_task_id=False`` builds a table missing
    the scoping column to exercise the graceful-empty path.
    """
    with kb.connect() as conn:
        if with_task_id:
            conn.execute(
                "CREATE TABLE decisions ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  task_id TEXT,"
                "  summary TEXT,"
                "  rationale TEXT,"
                "  created_at INTEGER"
                ")"
            )
        else:
            conn.execute(
                "CREATE TABLE decisions (id INTEGER PRIMARY KEY, note TEXT)"
            )
        conn.commit()


def _insert_decision(task_id: str, summary: str, rationale: str, created_at: int) -> None:
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO decisions (task_id, summary, rationale, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, summary, rationale, created_at),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# DB layer — context field
# ---------------------------------------------------------------------------


def test_create_task_stores_context(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="T", body="do the work",
            context="why: operator asked for it",
        )
        task = kb.get_task(conn, tid)
    assert task.context == "why: operator asked for it"
    # Context is a SEPARATE field from the work description.
    assert task.body == "do the work"


def test_context_whitespace_collapses_to_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T", context="   \n  ")
        task = kb.get_task(conn, tid)
    assert task.context is None


def test_create_task_without_context_is_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        task = kb.get_task(conn, tid)
    assert task.context is None


def test_set_task_context_set_clear_idempotent_and_unknown(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")

        ok, err = kb.set_task_context(conn, tid, "background story")
        assert ok and err is None
        assert kb.get_task(conn, tid).context == "background story"

        # Idempotent: same value again is a no-op success.
        ok, err = kb.set_task_context(conn, tid, "background story")
        assert ok and err is None

        # Empty string clears back to NULL.
        ok, err = kb.set_task_context(conn, tid, "")
        assert ok and err is None
        assert kb.get_task(conn, tid).context is None

        # Unknown task id fails cleanly.
        ok, err = kb.set_task_context(conn, "t_nope", "x")
        assert not ok
        assert "not found" in (err or "")


# ---------------------------------------------------------------------------
# DB layer — defensive decisions read
# ---------------------------------------------------------------------------


def test_list_task_decisions_no_table_returns_empty(kanban_home):
    with kb.connect() as conn:
        assert kb.list_task_decisions(conn, "t_any") == []


def test_list_task_decisions_missing_task_id_col_returns_empty(kanban_home):
    _make_decisions_table(with_task_id=False)
    with kb.connect() as conn:
        assert kb.list_task_decisions(conn, "t_any") == []


def test_list_task_decisions_returns_scoped_rows_newest_first(kanban_home):
    _make_decisions_table(with_task_id=True)
    _insert_decision("t_a", "older", "r1", created_at=100)
    _insert_decision("t_a", "newer", "r2", created_at=200)
    _insert_decision("t_b", "other card", "r3", created_at=300)

    with kb.connect() as conn:
        rows = kb.list_task_decisions(conn, "t_a")

    # Scoped to t_a only, newest-first by created_at.
    assert [r["summary"] for r in rows] == ["newer", "older"]
    assert all(r["task_id"] == "t_a" for r in rows)


# ---------------------------------------------------------------------------
# Plugin REST surface
# ---------------------------------------------------------------------------


def test_api_create_and_get_task_context(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Card", "body": "work", "context": "the why"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    assert r.json()["task"]["context"] == "the why"

    got = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert got.status_code == 200
    assert got.json()["task"]["context"] == "the why"
    # Empty by default — the sibling decisions table isn't present.
    assert got.json()["decisions"] == []


def test_api_patch_sets_and_clears_context(client):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]

    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}", json={"context": "added later"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task"]["context"] == "added later"

    # Empty string clears it back to null.
    r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"context": ""})
    assert r.status_code == 200
    assert r.json()["task"]["context"] is None


def test_api_get_task_surfaces_related_decisions(client):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]

    _make_decisions_table(with_task_id=True)
    _insert_decision(tid, "chose sqlite", "simplest durable store", created_at=100)

    got = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert got.status_code == 200
    decisions = got.json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["summary"] == "chose sqlite"
    assert decisions[0]["task_id"] == tid
