"""Tests for size-based model/effort selection (task t_36f2761f).

Covers the pure selection rule (small / normal / substantial + manual-override
priority + executor availability), the create_task integration (manual pins are
never overwritten; the auto verdict is recorded as an event), the
auto_model_select toggle, and the claude-sonnet-5 pricing entry the metric
depends on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import task_sizing as ts


# ---------------------------------------------------------------------------
# Pure classifier + selector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Правка одной строки CSS", ts.SMALL),
    ("Fix typo in the README", ts.SMALL),
    ("Обнови документацию по API", ts.SMALL),
    ("Rename the constant MAX_FOO", ts.SMALL),
    ("Add a submit button to the settings form", ts.NORMAL),
    ("Show the user's avatar on the profile page", ts.NORMAL),
    ("Почини баг: воркер падает при реклейме", ts.SUBSTANTIAL),
    ("Fix bug where dispatch deadlocks under load", ts.SUBSTANTIAL),
    ("Refactor the pricing module across multiple files", ts.SUBSTANTIAL),
    ("Migrate the kanban schema to add a column", ts.SUBSTANTIAL),
])
def test_classify_by_title(title, expected):
    cls, _reason = ts.classify_task(title)
    assert cls == expected


def test_substantial_wins_over_small_when_both_match():
    # "css" (small) + "fix bug" (substantial) both present → substantial wins
    # (mis-sizing real work down is the worse failure).
    cls, _ = ts.classify_task("Fix bug in the CSS theme loader")
    assert cls == ts.SUBSTANTIAL


def test_category_forces_class():
    assert ts.classify_task("do the thing", category="docs")[0] == ts.SMALL
    assert ts.classify_task("do the thing", category="architecture")[0] == ts.SUBSTANTIAL
    assert ts.classify_task("do the thing", category="bugfix")[0] == ts.SUBSTANTIAL


def test_size_hint_dominates():
    # Prose says small, but an upstream hint says strong → substantial.
    cls, reason = ts.classify_task("fix typo", size_hint="strong")
    assert cls == ts.SUBSTANTIAL
    assert "hint" in reason
    assert ts.classify_task("rewrite everything", size_hint="cheap")[0] == ts.SMALL


def test_ambiguous_defaults_to_normal():
    assert ts.classify_task("Wire up the settings panel")[0] == ts.NORMAL


def test_select_small_freezes_cheap_and_low_effort():
    sel = ts.select_model_and_effort(
        "Fix typo in README", model_map={}, executor="claude-code",
    )
    assert sel.task_class == ts.SMALL
    assert sel.model == "claude-haiku-4-5"
    assert sel.effort == "low"
    assert sel.supported is True


def test_select_normal_freezes_mid_and_medium_effort():
    sel = ts.select_model_and_effort(
        "Add a submit button", model_map={}, executor="claude-code",
    )
    assert sel.task_class == ts.NORMAL
    assert sel.model == "claude-sonnet-5"
    assert sel.effort == "medium"


def test_select_substantial_freezes_nothing():
    sel = ts.select_model_and_effort(
        "Fix bug in dispatch deadlock", model_map={}, executor="claude-code",
    )
    assert sel.task_class == ts.SUBSTANTIAL
    assert sel.model is None
    assert sel.effort is None
    # display_model still populated so the UI can show what it will run on.
    assert sel.display_model == "claude-opus-4-8"


def test_model_map_override_wins_over_default_ladder():
    sel = ts.select_model_and_effort(
        "Fix typo", model_map={"cheap": "claude-haiku-4-5-20251001"},
        executor="hermes-worker",
    )
    assert sel.model == "claude-haiku-4-5-20251001"


def test_unsupported_model_on_claude_code_falls_back():
    # A cheap role that maps to a non-Claude model the ACP claude-code executor
    # can't run → drop the freeze, report unsupported + the reason.
    sel = ts.select_model_and_effort(
        "Fix typo", model_map={"cheap": "glm-5.2"}, executor="claude-code",
    )
    assert sel.model is None
    assert sel.effort is None
    assert sel.supported is False
    assert "not supported" in sel.reason


def test_hermes_worker_accepts_any_model():
    sel = ts.select_model_and_effort(
        "Fix typo", model_map={"cheap": "glm-5.2"}, executor="hermes-worker",
    )
    assert sel.model == "glm-5.2"
    assert sel.supported is True


def test_default_ladder_models_are_priced():
    # Every model the selector can freeze MUST have pricing or the savings
    # metric silently counts $0.
    from agent import usage_pricing as up
    for role, model in ts.DEFAULT_ROLE_MODELS.items():
        assert up.has_known_pricing(model, provider="anthropic"), (
            f"role {role} model {model} has no pricing entry"
        )


# ---------------------------------------------------------------------------
# create_task integration
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB (never the live board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(conn, tid):
    return kb.get_task(conn, tid)


def _autoselect_event(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'model_autoselected' ORDER BY id LIMIT 1",
        (tid,),
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else None


def test_small_task_autoselects_cheap_model(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Fix typo in README", created_by="tester",
            executor="claude-code",
        )
        t = _task(conn, tid)
        assert t.model_override == "claude-haiku-4-5"
        assert t.effort_override == "low"
        ev = _autoselect_event(conn, tid)
        assert ev and ev["class"] == ts.SMALL and ev["source"] == "auto"


def test_substantial_task_records_event_but_freezes_nothing(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Fix bug: dispatch deadlocks", created_by="tester",
            executor="claude-code",
        )
        t = _task(conn, tid)
        assert t.model_override is None
        assert t.effort_override is None
        ev = _autoselect_event(conn, tid)
        assert ev and ev["class"] == ts.SUBSTANTIAL


def test_manual_model_override_not_overwritten(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Fix typo in README", created_by="tester",
            executor="claude-code", model_override="claude-opus-4-8",
        )
        t = _task(conn, tid)
        assert t.model_override == "claude-opus-4-8"
        # No auto verdict recorded — the manual pin short-circuits selection.
        assert _autoselect_event(conn, tid) is None


def test_manual_effort_override_not_overwritten(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Fix typo in README", created_by="tester",
            executor="claude-code", effort_override="high",
        )
        t = _task(conn, tid)
        assert t.effort_override == "high"
        assert t.model_override is None  # selection skipped entirely
        assert _autoselect_event(conn, tid) is None


def test_autoselect_disabled_freezes_nothing(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "auto_model_select_enabled", lambda board=None: False)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Fix typo in README", created_by="tester",
            executor="claude-code",
        )
        t = _task(conn, tid)
        assert t.model_override is None
        assert t.effort_override is None
        assert _autoselect_event(conn, tid) is None


# ---------------------------------------------------------------------------
# toggle
# ---------------------------------------------------------------------------

def test_toggle_defaults_on(kanban_home):
    assert kb.auto_model_select_enabled() is True


def test_toggle_board_metadata_overrides(kanban_home):
    kb.write_board_metadata(None, auto_model_select=False)
    assert kb.auto_model_select_enabled() is False
