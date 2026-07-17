"""Regression tests for t_0831813e — create/decompose must never leave a
dispatchable task (ready/todo) unowned.

An unassigned ready task is silently skipped by the dispatcher ("Skipped
unassigned"), stalling the queue with no visible signal. The root fix assigns
an owner at creation / decomposition time: inherit the parent's owner, else the
configured ``kanban.default_assignee``. A defensive WARNING in the dispatcher
surfaces any row that still reaches ``ready`` unowned.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB (never touches the live board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _assignee(conn, task_id):
    return conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["assignee"]


# ---- create_task -----------------------------------------------------------


def test_ready_task_gets_configured_default_at_create(kanban_home, monkeypatch):
    """The core fix: a ready task created with no assignee inherits the
    configured board default AT CREATE TIME, so it is owned before the
    dispatcher ever sees it."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "default")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t1", assignee=None)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert _assignee(conn, tid) == "default"


def test_ready_task_stays_unassigned_without_configured_default(kanban_home, monkeypatch):
    """Backward compatible: with no default configured and no parent to inherit
    from, the row stays unassigned (the dispatcher's fallback + WARNING net
    still applies)."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: None)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t1", assignee=None)
        assert _assignee(conn, tid) is None


def test_child_inherits_parent_assignee_over_default(kanban_home, monkeypatch):
    """A child of an owned parent inherits that owner — even when a different
    board default is configured, parent inheritance wins."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "boarddefault")
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        # Parent not done -> child lands in 'todo', which is still a
        # dispatchable lane and so must be owned.
        child = kb.create_task(conn, title="child", assignee=None, parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        assert _assignee(conn, child) == "alice"


def test_explicit_assignee_is_never_overridden(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "default")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t1", assignee="bob")
        assert _assignee(conn, tid) == "bob"


def test_triage_task_is_exempt_from_default_assignment(kanban_home, monkeypatch):
    """Triage tasks are not dispatched; they acquire an owner at decompose/take
    time, so create must leave them unassigned even with a default set."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "default")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idea", assignee=None, triage=True)
        assert kb.get_task(conn, tid).status == "triage"
        assert _assignee(conn, tid) is None


# ---- decompose -------------------------------------------------------------


def test_decompose_children_get_configured_default(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "default")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee=None, triage=True)
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None,
            children=[{"title": "a"}, {"title": "b"}],
        )
        assert child_ids and len(child_ids) == 2
        for cid in child_ids:
            assert _assignee(conn, cid) == "default"


def test_decompose_children_inherit_root_assignee(kanban_home, monkeypatch):
    """When the root is owned, children inherit the root's owner rather than a
    (different) global default."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: "boarddefault")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee=None, triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='carol' WHERE id=?", (root,))
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None, children=[{"title": "a"}],
        )
        assert _assignee(conn, child_ids[0]) == "carol"


def test_decompose_children_stay_unassigned_without_default(kanban_home, monkeypatch):
    """No default, no owned root -> children stay unassigned (preserves the
    'unassigned until take' path for take v2)."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: None)
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee=None, triage=True)
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee=None, children=[{"title": "a"}],
        )
        assert _assignee(conn, child_ids[0]) is None


# ---- dispatcher WARNING (defensive net) ------------------------------------


def test_dispatch_warns_on_unassigned_ready(kanban_home, monkeypatch, caplog):
    """A ready task that reached the dispatcher unowned (no default fallback)
    must emit a visible WARNING, not a silent skip."""
    monkeypatch.setattr(kb, "configured_default_assignee", lambda: None)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="orphan", assignee=None)
        assert _assignee(conn, tid) is None
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            res = kb.dispatch_once(conn, spawn_fn=lambda *_: 1234, dry_run=False)
    assert tid in res.skipped_unassigned
    assert any(
        "no assignee" in rec.getMessage() and tid in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


# ---- resolver reads config -------------------------------------------------


def test_configured_default_assignee_reads_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"default_assignee": "Bob"}},
    )
    # Canonicalised (lowercased) profile name.
    assert kb.configured_default_assignee() == "bob"


def test_configured_default_assignee_blank_is_none(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"default_assignee": "   "}},
    )
    assert kb.configured_default_assignee() is None
