"""Tests for project-vault decision registry writes."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "zeus"
    (repo / "knowledge").mkdir(parents=True)
    (repo / "knowledge" / "decisions.md").write_text("# Decision registry\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_record_task_decision_appends_a_searchable_git_backed_entry(tmp_path):
    from hermes_cli.project_vault import record_task_decision

    repo = _git_repo(tmp_path)
    path = record_task_decision(
        project_root=repo,
        board="ra",
        task_id="t_123",
        decision={
            "summary": "Use Markdown as the source of truth",
            "rationale": "It is reviewable and versioned in Git.",
            "links": ["Projects/Zeus/knowledge/README.md"],
        },
    )

    assert path == repo / "knowledge" / "decisions.md"
    text = path.read_text(encoding="utf-8")
    assert "## t_123 — Use Markdown as the source of truth" in text
    assert "- Task: `kanban:ra/t_123`" in text
    assert "- Rationale: It is reviewable and versioned in Git." in text
    assert "- Related: `Projects/Zeus/knowledge/README.md`" in text
    assert "<!-- kanban-decision:t_123 -->" in text


def test_record_task_decision_is_idempotent_per_task(tmp_path):
    from hermes_cli.project_vault import record_task_decision

    repo = _git_repo(tmp_path)
    decision = {"summary": "Keep a decision registry", "rationale": "Avoid duplicate records."}
    record_task_decision(project_root=repo, board="ra", task_id="t_same", decision=decision)
    record_task_decision(project_root=repo, board="ra", task_id="t_same", decision=decision)

    text = (repo / "knowledge" / "decisions.md").read_text(encoding="utf-8")
    assert text.count("<!-- kanban-decision:t_same -->") == 1


@pytest.mark.parametrize(
    "decision",
    [
        {},
        {"summary": "", "rationale": "because"},
        {"summary": "summary", "rationale": ""},
        {"summary": "summary", "rationale": "because", "links": [""]},
    ],
)
def test_record_task_decision_rejects_empty_or_invalid_entries(tmp_path, decision):
    from hermes_cli.project_vault import DecisionValidationError, record_task_decision

    repo = _git_repo(tmp_path)
    with pytest.raises(DecisionValidationError):
        record_task_decision(project_root=repo, board="ra", task_id="t_invalid", decision=decision)


def test_record_task_decision_refuses_registry_outside_git(tmp_path):
    from hermes_cli.project_vault import DecisionRegistryError, record_task_decision

    root = tmp_path / "not-a-repo"
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "decisions.md").write_text("# Decision registry\n", encoding="utf-8")
    with pytest.raises(DecisionRegistryError, match="Git"):
        record_task_decision(
            project_root=root,
            board="ra",
            task_id="t_no_git",
            decision={"summary": "x", "rationale": "y"},
        )
