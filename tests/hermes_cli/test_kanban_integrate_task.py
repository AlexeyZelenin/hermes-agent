"""End-to-end tests for the merge-queue landing step (`kb.integrate_task`).

Each test builds a throwaway kanban home (temp HERMES_HOME) plus a throwaway
git anchor repo under ``tmp_path`` — no host state, no real board, no real
``main``. The landing-gate command is faked (``true``/``false``) so no real
suite runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.trunk_integrator import Outcome


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo, *args, check=True):
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr or res.stdout}")
    return res


def _anchor_repo(tmp_path: Path) -> Path:
    r = tmp_path / "anchor"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "main")
    (r / "README.md").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _done_worktree_task(conn, repo: Path, filename: str, text: str):
    """Create a done task whose branch + worktree hold one commit off main."""
    branch = "zeus/t_work"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"work: {filename}")
    _git(repo, "checkout", "-q", "main")

    wt = repo / ".worktrees" / "placeholder"  # real path set after we know tid
    tid = kb.create_task(
        conn, title="work", assignee="worker",
        workspace_kind="worktree", branch_name=branch,
    )
    wt = repo / ".worktrees" / tid
    _git(repo, "worktree", "add", "-q", str(wt), branch)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='done', workspace_path=? WHERE id=?",
            (str(wt), tid),
        )
    return tid, branch, wt


def test_green_land_merges_prunes_and_lands(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _done_worktree_task(conn, repo, "a.txt", "alpha\n")
        res = kb.integrate_task(conn, tid, test_cmd=["true"])

    assert res.outcome is Outcome.LANDED
    # Branch landed on trunk.
    _git(repo, "checkout", "-q", "main")
    assert (repo / "a.txt").read_text() == "alpha\n"
    # Branch + worktree pruned (branch hygiene).
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode != 0
    assert not wt.exists()
    # Task stays done.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"


def test_red_suite_reopens_blocked_and_preserves_branch(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _done_worktree_task(conn, repo, "a.txt", "alpha\n")
        main_before = _git(repo, "rev-parse", "main").stdout.strip()
        res = kb.integrate_task(conn, tid, test_cmd=["false"])

    assert res.outcome is Outcome.TESTS_FAILED
    # Trunk NOT advanced; branch + worktree preserved (no work lost).
    assert _git(repo, "rev-parse", "main").stdout.strip() == main_before
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode == 0
    assert wt.exists()
    # Task reopened as blocked.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_conflict_reopens_blocked(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _done_worktree_task(conn, repo, "shared.txt", "from-branch\n")
        # Advance main to touch the SAME file -> guaranteed conflict.
        _git(repo, "checkout", "-q", "main")
        (repo / "shared.txt").write_text("from-main\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "main touches shared")
        res = kb.integrate_task(conn, tid, test_cmd=["true"])

    assert res.outcome is Outcome.CONFLICT
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode == 0
    # No merge left dangling in the anchor.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_idempotent_reland_is_noop(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _done_worktree_task(conn, repo, "a.txt", "alpha\n")
        first = kb.integrate_task(conn, tid, test_cmd=["true"])
        assert first.outcome is Outcome.LANDED
        # Recreate the (already-merged) branch to prove reland is a safe no-op.
        _git(repo, "branch", branch, "main")
        second = kb.integrate_task(conn, tid, test_cmd=["true"])

    assert second.outcome is Outcome.ALREADY_LANDED
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"


def test_task_without_branch_is_noop(kanban_home, tmp_path):
    _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-branch", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        res = kb.integrate_task(conn, tid, test_cmd=["true"])
    assert res.outcome is Outcome.NO_BRANCH
