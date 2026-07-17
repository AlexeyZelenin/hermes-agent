"""Tests for the trunk integrator (merge-queue) engine.

Every test builds its own throwaway git repo under ``tmp_path`` — no host
state, no real ``main``, no network. Tests inject a fake landing-gate command
(``["true"]`` / ``["false"]``) so no real suite runs.
"""

import subprocess
import threading
from pathlib import Path

import pytest

from hermes_cli import trunk_integrator as ti
from hermes_cli.trunk_integrator import (
    IntegrationResult,
    Outcome,
    TrunkLock,
    integrate_branch,
    resolve_trunk_ref,
)


def _git(repo, *args, check=True):
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr or res.stdout}")
    return res


def _commit(repo, name, text, msg=None):
    (Path(repo) / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg or f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "main")
    _commit(r, "README.md", "base\n", "base")
    return r


def _make_task_branch(repo, branch, filename, text):
    """Create ``branch`` off main with one commit adding ``filename``."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", branch)
    sha = _commit(repo, filename, text, f"work on {branch}")
    _git(repo, "checkout", "-q", "main")
    return sha


# ── resolve_trunk_ref ────────────────────────────────────────────────────────

def test_resolve_trunk_prefers_main(repo):
    assert resolve_trunk_ref(repo) == "main"


def test_resolve_trunk_falls_back_to_master(tmp_path):
    r = tmp_path / "m"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "master")
    _commit(r, "a", "a\n")
    assert resolve_trunk_ref(r) == "master"


def test_resolve_trunk_none_when_unconventional(tmp_path):
    r = tmp_path / "u"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "trunk")
    _commit(r, "a", "a\n")
    assert resolve_trunk_ref(r) is None


# ── Outcome semantics ────────────────────────────────────────────────────────

def test_outcome_landed_property():
    assert Outcome.LANDED.landed
    assert Outcome.ALREADY_LANDED.landed
    assert not Outcome.CONFLICT.landed
    assert not Outcome.TESTS_FAILED.landed


def test_outcome_should_block_property():
    assert Outcome.CONFLICT.should_block
    assert Outcome.TESTS_FAILED.should_block
    assert not Outcome.LANDED.should_block
    assert not Outcome.ALREADY_LANDED.should_block


# ── integrate_branch: happy path ─────────────────────────────────────────────

def test_clean_merge_lands_and_advances_trunk(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    res = integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    assert res.outcome is Outcome.LANDED
    assert res.merged_sha
    # trunk now contains the branch's file
    _git(repo, "checkout", "-q", "main")
    assert (repo / "a.txt").read_text() == "alpha\n"
    # branch is now an ancestor of trunk
    assert _git(repo, "merge-base", "--is-ancestor", "zeus/t_a", "main").returncode == 0


def test_land_leaves_primary_on_trunk(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    _git(repo, "checkout", "-q", "zeus/t_a")  # primary parked elsewhere
    integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_merge_commit_is_no_ff(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    res = integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    parents = _git(repo, "rev-list", "--parents", "-n", "1", res.merged_sha).stdout.split()
    assert len(parents) == 3, "landed commit should be a 2-parent merge commit"


# ── integrate_branch: idempotency ────────────────────────────────────────────

def test_already_landed_is_noop(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    before = _git(repo, "rev-parse", "main").stdout.strip()
    res = integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    assert res.outcome is Outcome.ALREADY_LANDED
    assert _git(repo, "rev-parse", "main").stdout.strip() == before


def test_branch_with_no_new_commits_is_already_landed(repo):
    # Branch that points at main with nothing new is already contained in trunk.
    _git(repo, "branch", "zeus/t_empty", "main")
    before = _git(repo, "rev-parse", "main").stdout.strip()
    res = integrate_branch(repo, "zeus/t_empty", test_cmd=["true"])
    assert res.outcome is Outcome.ALREADY_LANDED
    assert _git(repo, "rev-parse", "main").stdout.strip() == before


def test_missing_branch(repo):
    res = integrate_branch(repo, "zeus/t_ghost", test_cmd=["true"])
    assert res.outcome is Outcome.NO_BRANCH


# ── integrate_branch: failure paths preserve work & restore state ────────────

def test_conflict_aborts_and_preserves_branch(repo):
    # Two branches editing the SAME line -> second conflicts after first lands.
    _make_task_branch(repo, "zeus/t_1", "shared.txt", "from-one\n")
    _make_task_branch(repo, "zeus/t_2", "shared.txt", "from-two\n")
    assert integrate_branch(repo, "zeus/t_1", test_cmd=["true"]).outcome is Outcome.LANDED
    main_after_first = _git(repo, "rev-parse", "main").stdout.strip()

    res = integrate_branch(repo, "zeus/t_2", test_cmd=["true"])
    assert res.outcome is Outcome.CONFLICT
    # Trunk unchanged by the failed land; branch still exists; no merge in progress.
    assert _git(repo, "rev-parse", "main").stdout.strip() == main_after_first
    assert _git(repo, "rev-parse", "--verify", "zeus/t_2").returncode == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_tests_failed_rolls_back_and_preserves_branch(repo):
    _make_task_branch(repo, "zeus/t_red", "a.txt", "alpha\n")
    before = _git(repo, "rev-parse", "main").stdout.strip()
    res = integrate_branch(repo, "zeus/t_red", test_cmd=["false"])
    assert res.outcome is Outcome.TESTS_FAILED
    assert res.test_returncode == 1
    # Trunk NOT advanced; branch intact; tree clean.
    assert _git(repo, "rev-parse", "main").stdout.strip() == before
    assert _git(repo, "rev-parse", "--verify", "zeus/t_red").returncode == 0
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert not (repo / "a.txt").exists()


def test_failure_restores_original_branch(repo):
    _make_task_branch(repo, "zeus/t_red", "a.txt", "alpha\n")
    _git(repo, "checkout", "-q", "-b", "feature/parked")
    res = integrate_branch(repo, "zeus/t_red", test_cmd=["false"])
    assert res.outcome is Outcome.TESTS_FAILED
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "feature/parked"


def test_dirty_anchor_is_refused(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    (repo / "README.md").write_text("locally-dirty\n")  # tracked change, uncommitted
    res = integrate_branch(repo, "zeus/t_a", test_cmd=["true"])
    assert res.outcome is Outcome.DIRTY_ANCHOR
    # We did not touch the dirty file.
    assert (repo / "README.md").read_text() == "locally-dirty\n"


def test_run_tests_false_skips_gate(repo):
    _make_task_branch(repo, "zeus/t_a", "a.txt", "alpha\n")
    res = integrate_branch(repo, "zeus/t_a", run_tests=False)
    assert res.outcome is Outcome.LANDED


def test_no_trunk_returns_error(tmp_path):
    r = tmp_path / "u"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "trunk")
    _commit(r, "a", "a\n")
    res = integrate_branch(r, "trunk", test_cmd=["true"])
    assert res.outcome is Outcome.ERROR


# ── TrunkLock ────────────────────────────────────────────────────────────────

def test_trunk_lock_serializes(repo):
    with TrunkLock(repo):
        with pytest.raises(TimeoutError):
            with TrunkLock(repo, timeout=0.3, poll=0.05):
                pass


def test_trunk_lock_releases(repo):
    with TrunkLock(repo, timeout=1):
        pass
    # Re-acquirable after release.
    with TrunkLock(repo, timeout=1):
        pass


def test_trunk_lock_blocks_then_acquires(repo):
    order = []

    def hold():
        with TrunkLock(repo, timeout=5):
            order.append("held")
            acquired.set()
            release.wait(timeout=5)

    acquired = threading.Event()
    release = threading.Event()
    t = threading.Thread(target=hold)
    t.start()
    assert acquired.wait(timeout=5), "holder thread never acquired the lock"
    # Second waiter times out while first still holds.
    with pytest.raises(TimeoutError):
        with TrunkLock(repo, timeout=0.3, poll=0.05):
            pass
    release.set()
    t.join(timeout=5)
    with TrunkLock(repo, timeout=2):
        order.append("acquired-after")
    assert order == ["held", "acquired-after"]


def test_integration_result_str_is_readable(repo):
    r = IntegrationResult(Outcome.LANDED, "zeus/t_a", "main", merged_sha="abc123def456")
    s = str(r)
    assert "landed" in s and "zeus/t_a" in s and "main" in s
