"""Smoke test: a project-linked kanban task runs in the project repo, so the
worker sees the project's own ``.claude/skills`` + ``CLAUDE.md`` from its cwd —
the board equivalent of the operator's manual ``sb`` / ``dw`` fish flows
(``cd <project> && claude …``). Task t_f21213bb.

What this proves end-to-end (no live Claude session needed):
  1. Linking a task to a project with a ``primary_path`` upgrades it from a
     throwaway ``scratch`` dir to a real git worktree anchored under the project
     repo (closes the "scratch-trap": tasks landing in an empty dir instead of
     the code).
  2. That resolved worktree is a genuine checkout of the repo, so the project's
     ``.claude/skills/<skill>/SKILL.md`` and ``CLAUDE.md`` are physically
     present at the worker's cwd. Claude Code loads project-level skills +
     CLAUDE.md from cwd (walking up), so they resolve exactly as in a manual
     session.
  3. The project's append-system-prompt is frozen onto the task (the ACP-path
     stand-in for ``claude --append-system-prompt``).

KNOWN BOUNDARY (Q5, verify with a live session, not assertable here): the ACP
worker runs with ``CLAUDE_CONFIG_DIR`` pinned to the leased subscription
"pocket" (``~/.claude-sub-<name>``), NOT the operator's ``~/.claude``. So
*user-level* ``~/.claude/plugins`` / skills do NOT load in a pocket — only the
project's own in-repo ``.claude/`` (from cwd, config-dir-independent) does. If a
pocket needs global plugins, install/symlink them into that pocket's config dir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _make_project_repo(root: Path) -> Path:
    """A throwaway git repo shaped like ``project.SimpleBusiness``: an in-repo
    ``.claude/skills`` + a ``CLAUDE.md``, one commit so worktrees can branch."""
    repo = root / "project.SimpleBusiness"
    skill = repo / ".claude" / "skills" / "sb-invoice"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# SB invoice skill\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("SimpleBusiness project rules.\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def kanban_conn(tmp_path):
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def test_project_task_cwd_sees_project_skills_and_claude_md(monkeypatch, kanban_conn, tmp_path):
    repo = _make_project_repo(tmp_path)
    monkeypatch.setattr(pdb, "projects_db_path", lambda: tmp_path / "projects.db")
    with pdb.connect_closing() as projects:
        pid = pdb.create_project(
            projects,
            name="SimpleBusiness",
            folders=[str(repo)],
            executor="claude-code",
            append_system_prompt="Source code is in ../../src/SimpleBusiness",
        )

    # A plain (scratch-default) task, linked to the project, must escape the
    # scratch-trap: it becomes a worktree anchored under the project repo.
    tid = kb.create_task(kanban_conn, title="SB smoke", project_id=pid)
    task = kb.get_task(kanban_conn, tid)
    assert task.workspace_kind == "worktree"

    workspace, _branch = kb._resolve_worktree_workspace(task, board=None)
    workspace = Path(workspace)

    # The worker's cwd is a real checkout of the project repo — project skills
    # and CLAUDE.md are reachable from cwd, just like the manual `sb` flow.
    assert (workspace / ".claude" / "skills" / "sb-invoice" / "SKILL.md").is_file()
    assert (workspace / "CLAUDE.md").is_file()

    # And the orientation prompt is frozen onto the task for the ACP session.
    assert task.append_system_prompt == "Source code is in ../../src/SimpleBusiness"


def test_unlinked_task_stays_in_scratch(kanban_conn):
    """Control: without a project link a task stays scratch (the trap) — proving
    it's the project link that anchors it at the code."""
    tid = kb.create_task(kanban_conn, title="unlinked")
    task = kb.get_task(kanban_conn, tid)
    assert task.workspace_kind == "scratch"
    assert task.project_id is None
