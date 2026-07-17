"""New task worktrees must branch from the trunk, not the incidental HEAD.

Root cause of branch sprawl: ``_ensure_git_worktree`` created new branches off
``HEAD`` — whatever the anchor checkout happened to be parked on. When the
anchor sat on a stale task branch, every new task rooted on that stale base.
These tests pin the fix: new branches root on ``main`` even when the anchor is
checked out elsewhere and behind.

Own throwaway repo per test; no host state.
"""

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db


def _git(repo, *args, check=True):
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr or res.stdout}")
    return res


def _commit(repo, name, text):
    (Path(repo) / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "main")
    _commit(r, "README.md", "base\n")
    return r


def test_new_branch_roots_on_main_not_stale_head(repo):
    # A stale branch off the base, then main advances past it.
    _git(repo, "checkout", "-q", "-b", "zeus/t_stale")
    # main gets a new commit the stale branch never sees.
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "on_main.txt", "main-only\n")
    # Park the anchor on the stale branch (the sprawl scenario).
    _git(repo, "checkout", "-q", "zeus/t_stale")

    target = repo / ".worktrees" / "t_new"
    kanban_db._ensure_git_worktree(repo, target, "zeus/t_new")

    # The new worktree must contain main's commit, proving it rooted on main
    # rather than the stale HEAD it was created from.
    assert (target / "on_main.txt").exists(), "new task branch did not root on trunk"
    assert (target / "README.md").exists()


def test_resolve_trunk_base_prefers_main(repo):
    assert kanban_db._resolve_trunk_base(repo) == "main"


def test_resolve_trunk_base_falls_back_to_head(tmp_path):
    r = tmp_path / "u"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "develop")
    _commit(r, "a", "a\n")
    assert kanban_db._resolve_trunk_base(r) == "HEAD"


def test_master_repo_roots_on_master(tmp_path):
    r = tmp_path / "m"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "master")
    _commit(r, "README.md", "base\n")
    _git(r, "checkout", "-q", "-b", "side")
    _git(r, "checkout", "-q", "master")
    _commit(r, "on_master.txt", "master-only\n")
    _git(r, "checkout", "-q", "side")

    target = r / ".worktrees" / "t_new"
    kanban_db._ensure_git_worktree(r, target, "zeus/t_new")
    assert (target / "on_master.txt").exists()
