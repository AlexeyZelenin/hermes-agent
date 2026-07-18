"""Tests for the vision gate in hermes_cli.kanban_specify.

The auxiliary LLM client is mocked and the vision doc is a tmp file pointed at
via ``HERMES_VISION_DOC`` — no network, no shipping-repo dependency. Proves the
triage->todo gate holds a card that fails the vision check and promotes one that
passes (or when no vision doc is present at all).
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def vision_doc(tmp_path, monkeypatch):
    p = tmp_path / "vision.md"
    p.write_text("# Vision\n\n## Что НЕ делаем\n- не полурешения\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_VISION_DOC", str(p))
    return p


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _patch_aux(content: str):
    mock_fn = MagicMock(return_value=_fake_aux_response(content))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


# --- verdict parsing (pure) -------------------------------------------------


def test_vision_verdict_reads_fits_false():
    parsed = {"title": "T", "body": "B", "vision": {"fits": False, "reason": "drift"}}
    assert spec._vision_verdict(parsed) == (False, "drift")


def test_vision_verdict_missing_key_is_none():
    assert spec._vision_verdict({"title": "T", "body": "B"}) is None
    assert spec._vision_verdict({"vision": "notadict"}) is None
    assert spec._vision_verdict(None) is None


# --- gate behaviour ---------------------------------------------------------


def test_gate_holds_card_that_fails_vision(kanban_home, vision_doc):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local repo and that's it", triage=True)
    content = jsonlib.dumps({
        "title": "Local repo only",
        "body": "**Goal** — half a thing",
        "vision": {"fits": False, "reason": "полурешение, вне видения"},
    })
    p, mock_fn = _patch_aux(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is False
    assert "vision-check failed" in outcome.reason
    # The vision doc was actually mixed into the planner's system prompt.
    sys_prompt = mock_fn.call_args.kwargs["messages"][0]["content"]
    assert "VISION CHECK" in sys_prompt
    # Card stays in Triage — not auto-promoted.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        comments = kb.list_comments(conn, tid)
    assert any("Vision-гейт" in c.body for c in comments)


def test_gate_promotes_card_that_passes_vision(kanban_home, vision_doc):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="finish the pipeline", triage=True)
    content = jsonlib.dumps({
        "title": "Finish the pipeline",
        "body": "**Goal** — a complete flow",
        "vision": {"fits": True, "reason": "advances the vision"},
    })
    p, _ = _patch_aux(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status in ("todo", "ready")


def test_gate_promotes_when_no_vision_doc(kanban_home, monkeypatch, tmp_path):
    # No vision doc anywhere -> pre-vision behaviour: promote as usual and never
    # ask the model for a verdict.
    monkeypatch.setenv("HERMES_VISION_DOC", str(tmp_path / "absent.md"))
    monkeypatch.setenv("HERMES_PROJECT_ROOT", str(tmp_path))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", triage=True)
    content = jsonlib.dumps({"title": "Refined", "body": "**Goal** — do it"})
    p, mock_fn = _patch_aux(content)
    with p:
        outcome = spec.specify_task(tid)

    assert outcome.ok is True
    sys_prompt = mock_fn.call_args.kwargs["messages"][0]["content"]
    assert "VISION CHECK" not in sys_prompt
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status in ("todo", "ready")
