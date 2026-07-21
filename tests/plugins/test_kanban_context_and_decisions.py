"""Typed card fields: context / question / user quotes, journal, decision links.

The card is a set of TYPED fields instead of one prose blob (t_cd5e8574 landed
``context``; t_a25cb2b3 adds the rest), so a reader can later reconstruct WHY
the task exists and HOW the work went:

  * ``tasks.context`` / ``tasks.question`` / ``tasks.user_quotes`` — the frame,
    the ask, and the operator's verbatim words, each separate from ``body``
    (the work description).
  * ``task_journal`` — append-only one-line-per-step work log.
  * ``task_decision_links`` — "this card grew out of that decision". Decisions
    live in a store this database cannot join against (the Roul plugin's
    ``roul.db``), so the owning store pushes a link carrying a snapshot of the
    question/answer.

Exercised at the DB layer (``kanban_db``), through the dashboard plugin's REST
surface, and in the worker context dump. The plugin router is attached to a
bare FastAPI app (mirroring ``test_kanban_dashboard_plugin.py``) so the REST
surface is testable without the full dashboard. All state is an isolated
per-test ``HERMES_HOME``.
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


# ---------------------------------------------------------------------------
# DB layer — typed text fields
# ---------------------------------------------------------------------------


def test_create_task_stores_typed_fields(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="T", body="do the work",
            context="why: operator asked for it",
            question="do we split the blob into fields?",
            user_quotes="«у каждой задачи должен быть контекст»",
        )
        task = kb.get_task(conn, tid)
    assert task.context == "why: operator asked for it"
    assert task.question == "do we split the blob into fields?"
    assert task.user_quotes == "«у каждой задачи должен быть контекст»"
    # Each is a SEPARATE field from the work description.
    assert task.body == "do the work"


@pytest.mark.parametrize("field", ["context", "question", "user_quotes"])
def test_typed_field_whitespace_collapses_to_none(kanban_home, field):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T", **{field: "   \n  "})
        task = kb.get_task(conn, tid)
    assert getattr(task, field) is None


def test_create_task_without_typed_fields_is_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        task = kb.get_task(conn, tid)
    assert (task.context, task.question, task.user_quotes) == (None, None, None)


@pytest.mark.parametrize(
    "field,setter_name",
    [
        ("context", "set_task_context"),
        ("question", "set_task_question"),
        ("user_quotes", "set_task_user_quotes"),
    ],
)
def test_setters_set_clear_idempotent_and_unknown(kanban_home, field, setter_name):
    setter = getattr(kb, setter_name)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")

        ok, err = setter(conn, tid, "some text")
        assert ok and err is None
        assert getattr(kb.get_task(conn, tid), field) == "some text"

        # Idempotent: same value again is a no-op success.
        ok, err = setter(conn, tid, "some text")
        assert ok and err is None

        # Empty string clears back to NULL.
        ok, err = setter(conn, tid, "")
        assert ok and err is None
        assert getattr(kb.get_task(conn, tid), field) is None

        # Unknown task id fails cleanly.
        ok, err = setter(conn, "t_nope", "x")
        assert not ok
        assert "not found" in (err or "")


def test_editing_a_typed_field_bumps_updated_at(kanban_home):
    """The touch trigger must watch the new columns, not just the old ones."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        conn.execute("UPDATE tasks SET updated_at = 1 WHERE id = ?", (tid,))
        conn.commit()
        kb.set_task_question(conn, tid, "what are we deciding?")
        assert kb.get_task(conn, tid).updated_at > 1


def test_set_task_text_field_rejects_unknown_column(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        with pytest.raises(ValueError):
            kb._set_task_text_field(conn, tid, "status", "done")


# ---------------------------------------------------------------------------
# DB layer — work journal
# ---------------------------------------------------------------------------


def test_journal_appends_in_chronological_order(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        assert kb.list_journal(conn, tid) == []
        kb.add_journal_entry(conn, tid, "read the schema", author="glm")
        kb.add_journal_entry(conn, tid, "added the columns", author="glm")
        rows = kb.list_journal(conn, tid)
    assert [r["entry"] for r in rows] == ["read the schema", "added the columns"]
    assert all(r["author"] == "glm" for r in rows)


def test_journal_limit_keeps_most_recent_in_order(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        for i in range(5):
            kb.add_journal_entry(conn, tid, f"step {i}")
        rows = kb.list_journal(conn, tid, limit=2)
    assert [r["entry"] for r in rows] == ["step 3", "step 4"]


def test_journal_rejects_empty_entry_and_unknown_task(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        with pytest.raises(ValueError):
            kb.add_journal_entry(conn, tid, "   ")
        with pytest.raises(ValueError):
            kb.add_journal_entry(conn, "t_nope", "orphan line")


def test_deleting_a_task_removes_its_journal_and_links(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        kb.add_journal_entry(conn, tid, "step")
        kb.link_decision_to_task(conn, tid, 1, question="q?")
        assert kb.delete_task(conn, tid)
        assert kb.list_journal(conn, tid) == []
        assert kb.list_task_decisions(conn, tid) == []


# ---------------------------------------------------------------------------
# DB layer — decision links
# ---------------------------------------------------------------------------


def test_link_decision_unknown_task_is_refused(kanban_home):
    with kb.connect() as conn:
        assert kb.link_decision_to_task(conn, "t_nope", 1, question="q?") is False


def test_link_decision_upserts_and_returns_newest_first(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        assert kb.link_decision_to_task(conn, tid, 1, question="older?")
        assert kb.link_decision_to_task(conn, tid, 2, question="newer?")
        # Re-pushing the SAME decision refreshes it instead of duplicating.
        assert kb.link_decision_to_task(
            conn, tid, 1, question="older?", answer="да", status="answered",
        )
        rows = kb.list_task_decisions(conn, tid)

    assert len(rows) == 2
    by_id = {r["decision_id"]: r for r in rows}
    assert by_id[1]["answer"] == "да"
    assert by_id[1]["status"] == "answered"
    assert by_id[2]["status"] == "open"


def test_link_decision_is_scoped_to_its_task(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="A")
        b = kb.create_task(conn, title="B")
        kb.link_decision_to_task(conn, a, 1, question="mine")
        kb.link_decision_to_task(conn, b, 2, question="theirs")
        assert [r["question"] for r in kb.list_task_decisions(conn, a)] == ["mine"]


def test_first_link_emits_event_and_refresh_does_not(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T")
        kb.link_decision_to_task(conn, tid, 1, question="q?")
        kb.link_decision_to_task(conn, tid, 1, question="q?", answer="a")
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("decision_linked") == 1


# ---------------------------------------------------------------------------
# Worker context — the typed fields must reach the worker
# ---------------------------------------------------------------------------


def test_worker_context_renders_typed_fields_journal_and_decisions(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="T", body="the work",
            context="the frame", question="the ask",
            user_quotes="«точная цитата»",
        )
        kb.link_decision_to_task(
            conn, tid, 7, question="sqlite или postgres?", answer="sqlite",
            status="answered",
        )
        kb.add_journal_entry(conn, tid, "schema landed", author="glm")
        text = kb.build_worker_context(conn, tid)

    assert "## Context" in text and "the frame" in text
    assert "## Question" in text and "the ask" in text
    assert "«точная цитата»" in text
    assert "sqlite или postgres?" in text and "sqlite" in text
    assert "schema landed" in text
    # Ordering: frame → ask → quotes → work.
    assert text.index("## Context") < text.index("## Question") < text.index("## Body")


def test_worker_context_omits_unset_typed_sections(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="T", body="the work")
        text = kb.build_worker_context(conn, tid)
    for head in ("## Context", "## Question", "## User quotes", "## Work journal"):
        assert head not in text


# ---------------------------------------------------------------------------
# Plugin REST surface
# ---------------------------------------------------------------------------


def test_api_create_and_get_task_typed_fields(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Card", "body": "work", "context": "the why",
            "question": "the ask", "user_quotes": "«как просил»",
        },
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    assert r.json()["task"]["question"] == "the ask"

    got = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert got.status_code == 200
    task = got.json()["task"]
    assert task["context"] == "the why"
    assert task["user_quotes"] == "«как просил»"
    # Nothing linked / logged yet.
    assert got.json()["decisions"] == []
    assert got.json()["journal"] == []


def test_api_patch_sets_and_clears_typed_fields(client):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]

    for field in ("context", "question", "user_quotes"):
        r = client.patch(
            f"/api/plugins/kanban/tasks/{tid}", json={field: "added later"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task"][field] == "added later"

        # Empty string clears it back to null.
        r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={field: ""})
        assert r.status_code == 200
        assert r.json()["task"][field] is None


def test_api_journal_append_and_read_back(client):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]

    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/journal",
        json={"entry": "разобрался со схемой"},
    )
    assert r.status_code == 200, r.text
    assert [e["entry"] for e in r.json()["journal"]] == ["разобрался со схемой"]

    got = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert [e["entry"] for e in got.json()["journal"]] == ["разобрался со схемой"]


def test_api_journal_rejects_empty_and_unknown_task(client):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]
    assert client.post(
        f"/api/plugins/kanban/tasks/{tid}/journal", json={"entry": "  "},
    ).status_code == 400
    assert client.post(
        "/api/plugins/kanban/tasks/t_nope/journal", json={"entry": "x"},
    ).status_code == 404


def test_api_get_task_surfaces_linked_decisions(client, kanban_home):
    tid = client.post(
        "/api/plugins/kanban/tasks", json={"title": "Card"},
    ).json()["task"]["id"]

    with kb.connect() as conn:
        kb.link_decision_to_task(
            conn, tid, 42, question="chose sqlite?", answer="simplest durable store",
            status="answered",
        )

    got = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert got.status_code == 200
    decisions = got.json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision_id"] == 42
    assert decisions[0]["answer"] == "simplest durable store"
    assert decisions[0]["task_id"] == tid
