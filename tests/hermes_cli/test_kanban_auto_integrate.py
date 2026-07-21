"""Dispatcher auto-merge of a done task's branch into trunk (t_ac38cb6d).

The merge is a DISPATCHER responsibility, not a final step of the worker's ACP
session: a worker routinely dies on a wait/verify turn before it could run a
"now merge your branch" step (the phantom pattern, t_222f1e4a). So
``complete_task`` only *arms* the branch at the done transition
(``integration_status='pending'``) and the dispatcher's per-tick
``sweep_integrations`` lands it — even if the worker crashed the instant after
it committed and completed.

Every test builds a throwaway kanban home (isolated ``HERMES_HOME``) plus a
throwaway git anchor repo under ``tmp_path`` — no host state, no real board, no
real ``main``, and the landing gate is skipped (git-only merge) exactly as the
dispatcher sweep runs it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


def _integration_status(conn, tid: str):
    return conn.execute(
        "SELECT integration_status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()[0]


def _running_worktree_task(conn, repo: Path):
    """Create + claim a worktree task with a fresh branch off main (at base).

    Order matches real dispatch: the worktree is materialized and its path is
    persisted BEFORE the claim, so the run's ``base_commit`` is the trunk tip —
    a commit made afterward reads as "this run landed work" for the DoD gate.
    """
    tid = kb.create_task(
        conn, title="work", assignee="worker",
        workspace_kind="worktree", created_by="test",
    )
    branch = f"roul/{tid}"
    kb.set_branch_name(conn, tid, branch)
    wt = repo / ".worktrees" / tid
    kb._ensure_git_worktree(repo, wt, branch)  # branch created off main @ base
    kb.set_workspace_path(conn, tid, str(wt))
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return tid, branch, wt


def _commit_on_branch(wt: Path, filename: str, text: str):
    (wt / filename).write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", f"work: {filename}")


# ---------------------------------------------------------------------------
# 1) done → branch auto-merged into main
# ---------------------------------------------------------------------------

def test_done_arms_marker_and_sweep_lands(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "a.txt", "alpha\n")
        branch_tip = _git(wt, "rev-parse", "HEAD").stdout.strip()

        # Worker completes: DoD gate passes (clean tree + a commit) and the
        # branch is armed for the dispatcher sweep.
        assert kb.complete_task(conn, tid, summary="done + committed") is True
        assert kb.get_task(conn, tid).status == "done"
        assert _integration_status(conn, tid) == "pending"

        landed, blocked = kb.sweep_integrations(conn)

    assert landed == [tid]
    assert blocked == []
    # Branch landed on main via fast-forward (strictly ahead → no merge commit).
    assert _git(repo, "rev-parse", "main").stdout.strip() == branch_tip
    assert _git(repo, "rev-parse", "HEAD^2", check=False).returncode != 0
    _git(repo, "checkout", "-q", "main")
    assert (repo / "a.txt").read_text() == "alpha\n"
    # Branch + worktree pruned; marker advanced to landed; task stays done.
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode != 0
    assert not wt.exists()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
        assert _integration_status(conn, tid) == "landed"


# ---------------------------------------------------------------------------
# 2) phantom: worker session died right after completing → still merged
# ---------------------------------------------------------------------------

def test_phantom_completed_then_session_died_still_lands(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    # Worker connection: commit + complete, then the session "dies" (connection
    # closes) before it could ever run a merge step.
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "a.txt", "alpha\n")
        assert kb.complete_task(conn, tid, summary="done") is True

    # A wholly separate dispatcher connection observes the armed done task and
    # lands it — the worker never touched trunk.
    with kb.connect_closing() as conn:
        landed, blocked = kb.sweep_integrations(conn)

    assert landed == [tid]
    _git(repo, "checkout", "-q", "main")
    assert (repo / "a.txt").read_text() == "alpha\n"
    assert not wt.exists()


# ---------------------------------------------------------------------------
# 3) merge conflict → task reopened blocked, NOT merged, branch preserved
# ---------------------------------------------------------------------------

def test_conflict_reopens_blocked_and_does_not_merge(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "shared.txt", "from-branch\n")
        assert kb.complete_task(conn, tid, summary="done") is True

        # Advance main to touch the SAME file → guaranteed merge conflict.
        _git(repo, "checkout", "-q", "main")
        (repo / "shared.txt").write_text("from-main\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "main touches shared")
        main_before = _git(repo, "rev-parse", "main").stdout.strip()

        landed, blocked = kb.sweep_integrations(conn)

    assert landed == []
    assert blocked == [tid]
    # Trunk NOT advanced; branch + worktree preserved (no work lost).
    assert _git(repo, "rev-parse", "main").stdout.strip() == main_before
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode == 0
    assert wt.exists()
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert _integration_status(conn, tid) == "blocked"


# ---------------------------------------------------------------------------
# 4) dirty DoD → completion refused, no marker, sweep never sees it
# ---------------------------------------------------------------------------

def test_dirty_dod_blocks_completion_so_nothing_lands(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        # Worker leaves an uncommitted change — the DoD gate must refuse.
        (wt / "leftover.txt").write_text("uncommitted\n")
        main_before = _git(repo, "rev-parse", "main").stdout.strip()

        with pytest.raises(kb.UncommittedWorkError):
            kb.complete_task(conn, tid, summary="done but dirty")

        # Task never reached done, so it was never armed.
        assert kb.get_task(conn, tid).status == "running"
        assert _integration_status(conn, tid) is None

        landed, blocked = kb.sweep_integrations(conn)

    assert landed == []
    assert blocked == []
    assert _git(repo, "rev-parse", "main").stdout.strip() == main_before


# ---------------------------------------------------------------------------
# 5) scope: a pre-existing done branch (never armed) is left untouched
# ---------------------------------------------------------------------------

def test_preexisting_done_branch_not_touched(kanban_home, tmp_path):
    """A branch completed before this shipped has integration_status NULL.

    The sweep must ignore it entirely — mirrors the already-stranded branches
    (t_2e1a2936 / t_b92abd8c / t_d27458bb) the operator handles by hand.
    """
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "a.txt", "alpha\n")
        # Force done directly (bypassing complete_task) so no marker is armed —
        # exactly the state of a task completed before auto-merge existed.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        assert _integration_status(conn, tid) is None
        main_before = _git(repo, "rev-parse", "main").stdout.strip()

        landed, blocked = kb.sweep_integrations(conn)

    assert landed == []
    assert blocked == []
    # Nothing merged; branch + worktree left intact for the operator.
    assert _git(repo, "rev-parse", "main").stdout.strip() == main_before
    assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode == 0
    assert wt.exists()


# ---------------------------------------------------------------------------
# 6) diverged-but-clean history → merge commit referencing the task id
# ---------------------------------------------------------------------------

def test_diverged_history_lands_with_task_referencing_merge_commit(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "a.txt", "alpha\n")
        assert kb.complete_task(conn, tid, summary="done") is True

        # Advance main on a DIFFERENT file → histories diverge, no conflict, so
        # a fast-forward is impossible and a merge commit is required.
        _git(repo, "checkout", "-q", "main")
        (repo / "b.txt").write_text("beta\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "main advances b")

        landed, blocked = kb.sweep_integrations(conn)

    assert landed == [tid]
    # A real merge commit (two parents) whose message references the task id.
    assert _git(repo, "rev-parse", "HEAD^2", check=False).returncode == 0
    msg = _git(repo, "log", "-1", "--pretty=%B", "main").stdout
    assert f"(task {tid})" in msg
    assert (repo / "a.txt").read_text() == "alpha\n"
    assert (repo / "b.txt").read_text() == "beta\n"


# ---------------------------------------------------------------------------
# 7) wired into the dispatcher tick: dispatch_once lands armed done tasks
# ---------------------------------------------------------------------------

def test_dispatch_once_runs_the_auto_merge_sweep(kanban_home, tmp_path):
    repo = _anchor_repo(tmp_path)
    with kb.connect_closing() as conn:
        tid, branch, wt = _running_worktree_task(conn, repo)
        _commit_on_branch(wt, "a.txt", "alpha\n")
        assert kb.complete_task(conn, tid, summary="done") is True

        # One dispatcher tick (no ready work to spawn) must land the branch.
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 0)

    assert tid in res.integrated
    assert res.integration_blocked == []
    _git(repo, "checkout", "-q", "main")
    assert (repo / "a.txt").read_text() == "alpha\n"
    assert not wt.exists()
