"""Tests for backlog categories — a per-board managed catalog (name + icon)
that tasks reference via ``tasks.category`` (hermes_cli.kanban_db + CLI).

Categories are a managed set, not free text: the planner auto-assigns from the
catalog and the dashboard groups the backlog by category with a consistent
per-category icon. NULL category = uncategorized.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (seeded catalog)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Catalog seeding + CRUD
# ---------------------------------------------------------------------------

def test_default_catalog_seeded(kanban_home):
    with kb.connect() as conn:
        cats = kb.list_categories(conn)
    keys = [c.key for c in cats]
    assert keys == [k for (k, _n, _i) in kb.DEFAULT_CATEGORIES]
    # Icons come through intact (the operator's original groups).
    by_key = {c.key: c for c in cats}
    assert by_key["engine-room"].icon == "🖥️"
    assert by_key["intelligence"].icon == "🧠"


def test_seed_is_not_reapplied_after_curation(kanban_home):
    with kb.connect() as conn:
        kb.remove_category(conn, "mechanics")
        assert {c.key for c in kb.list_categories(conn)} == {
            "engine-room", "product", "intelligence"
        }
        # Re-running the seed must NOT resurrect the removed default: a
        # non-empty catalog is operator-curated and left alone.
        kb._seed_default_categories(conn)
        assert "mechanics" not in {c.key for c in kb.list_categories(conn)}


def test_upsert_create_and_update(kanban_home):
    with kb.connect() as conn:
        cat = kb.upsert_category(conn, "ops", name="Ops", icon="🛠", sort=5)
        assert (cat.key, cat.name, cat.icon, cat.sort) == ("ops", "Ops", "🛠", 5)
        # Partial update leaves omitted fields intact.
        cat2 = kb.upsert_category(conn, "ops", icon="🧰")
        assert (cat2.name, cat2.icon, cat2.sort) == ("Ops", "🧰", 5)


def test_upsert_requires_name_and_icon_on_create(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError):
            kb.upsert_category(conn, "nope", name="No icon")
        with pytest.raises(ValueError):
            kb.upsert_category(conn, "nope", icon="🚫")


def test_invalid_category_key_rejected(kanban_home):
    with kb.connect() as conn:
        # Note: keys are lowercased first, so "UPPER" is valid (→ "upper").
        for bad in ("Has Space", "with_underscore", "bad!char", "", "a" * 41):
            with pytest.raises(ValueError):
                kb.upsert_category(conn, bad, name="x", icon="x")


def test_remove_category_clears_referring_tasks(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", category="product")
        assert kb.get_task(conn, tid).category == "product"
        assert kb.remove_category(conn, "product") is True
        # Task loses the dangling reference rather than keeping a ghost key.
        assert kb.get_task(conn, tid).category is None
        # Removing an unknown key is a no-op False.
        assert kb.remove_category(conn, "product") is False


# ---------------------------------------------------------------------------
# Per-task assignment
# ---------------------------------------------------------------------------

def test_set_task_category_valid(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        ok, err = kb.set_task_category(conn, tid, "engine-room", actor="op")
        assert (ok, err) == (True, None)
        assert kb.get_task(conn, tid).category == "engine-room"
        assert "categorized" in [e.kind for e in kb.list_events(conn, tid)]


def test_set_task_category_unknown_rejected(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        ok, err = kb.set_task_category(conn, tid, "does-not-exist")
        assert ok is False
        assert "unknown category" in (err or "")
        assert kb.get_task(conn, tid).category is None


def test_set_task_category_clear(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", category="mechanics")
        ok, err = kb.set_task_category(conn, tid, None)
        assert (ok, err) == (True, None)
        assert kb.get_task(conn, tid).category is None


def test_set_task_category_unknown_task(kanban_home):
    with kb.connect() as conn:
        ok, err = kb.set_task_category(conn, "t_deadbeef", "product")
        assert ok is False
        assert "not found" in (err or "")


# ---------------------------------------------------------------------------
# create_task + decompose plumbing
# ---------------------------------------------------------------------------

def test_create_task_with_valid_category(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", category="intelligence")
        assert kb.get_task(conn, tid).category == "intelligence"


def test_create_task_unknown_category_dropped_to_null(kanban_home):
    with kb.connect() as conn:
        # Unknown key must not fail the create — task is just uncategorized.
        tid = kb.create_task(conn, title="t", assignee="a", category="bogus")
        assert kb.get_task(conn, tid).category is None


def test_decompose_assigns_child_categories(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="epic", assignee="a", triage=True)
        children = [
            {"title": "infra bit", "category": "engine-room", "parents": []},
            {"title": "ui bit", "category": "product", "parents": []},
            {"title": "loose bit", "category": "not-a-real-key", "parents": []},
        ]
        ids = kb.decompose_triage_task(
            conn, root, root_assignee=None, children=children, author="op",
        )
        cats = [kb.get_task(conn, i).category for i in ids]
        # Valid keys stick; the bogus one falls back to NULL, not the whole
        # fan-out failing.
        assert cats == ["engine-room", "product", None]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _run(args_ns):
    return kb_cli.kanban_command(args_ns)


def test_cli_category_list_add_rm(kanban_home, capsys):
    ns = argparse.Namespace(
        kanban_action="category", category_action="add",
        key="ops", name="Ops", icon="🛠", sort=None, json=True, board=None,
    )
    assert _run(ns) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["key"] == "ops" and out["icon"] == "🛠"

    ns = argparse.Namespace(
        kanban_action="category", category_action="list", json=True, board=None,
    )
    assert _run(ns) == 0
    listing = json.loads(capsys.readouterr().out)
    assert "ops" in [c["key"] for c in listing]

    ns = argparse.Namespace(
        kanban_action="category", category_action="rm", key="ops", board=None,
    )
    assert _run(ns) == 0
    capsys.readouterr()


def test_cli_set_category(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
    ns = argparse.Namespace(
        kanban_action="set-category", task_id=tid, category="product",
        json=False, board=None,
    )
    assert _run(ns) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).category == "product"

    # 'none' clears.
    ns = argparse.Namespace(
        kanban_action="set-category", task_id=tid, category="none",
        json=False, board=None,
    )
    assert _run(ns) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).category is None


def test_cli_set_category_unknown_returns_error(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
    ns = argparse.Namespace(
        kanban_action="set-category", task_id=tid, category="ghost",
        json=False, board=None,
    )
    assert _run(ns) == 1
