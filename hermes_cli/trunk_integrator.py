"""Trunk integrator (merge-queue) for kanban task branches.

Root-cause fix for branch sprawl: instead of every completed task leaving its
work stranded on a ``zeus/t_*`` / ``ra/t_*`` branch that no one ever merges,
the integrator merges each finished branch into the local trunk (``main``),
runs the test suite on the **merged result**, and lands only when green. A
merge conflict or a red suite leaves the branch untouched and hands the caller
a typed failure so the task can be blocked without losing work.

Two properties make this safe to run automatically:

* **Serialized landings.** All landings for one repo go through a single
  advisory file lock (:class:`TrunkLock`). Two integrator passes never race on
  the trunk ref, so the "merged then tested" result a land is based on is the
  result that actually lands.
* **Idempotent.** Re-integrating an already-merged branch is a no-op
  (:attr:`Outcome.ALREADY_LANDED`), so a crashed/retried pass never
  double-merges.

The merge happens on the anchor repo's **primary checkout** (the board's
``default_workdir``), checked out onto trunk under the lock — the same thing a
human does by hand, and the only way to advance a checked-out branch without
leaving its index inconsistent (``git update-ref`` on a checked-out branch
does not touch the working tree/index, so it would strand a stale index).
Per-task work lives in separate ``.worktrees/<id>`` checkouts on their own
branches, so the primary is free to be the integration surface.

This is the automated form of the manual nightly consolidation that produced
``integration/night-2026-07-18``.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

_log = logging.getLogger(__name__)

# Trunk branch names tried, in order, when the caller does not name one.
DEFAULT_TRUNK_CANDIDATES = ("main", "master")

# Default landing-gate test command, run from the merged trunk checkout. This
# mirrors CI (see scripts/run_tests.sh); callers may pass a scoped command.
DEFAULT_TEST_CMD: tuple[str, ...] = ("scripts/run_tests.sh",)


class Outcome(str, Enum):
    """Terminal outcome of a single branch integration attempt."""

    LANDED = "landed"                # merged, tested green, trunk advanced
    ALREADY_LANDED = "already_landed"  # branch already an ancestor of trunk (incl. behind/equal)
    NO_BRANCH = "no_branch"          # branch does not exist
    CONFLICT = "conflict"            # merge hit a conflict (aborted, branch kept)
    TESTS_FAILED = "tests_failed"    # merged clean but suite went red (rolled back)
    DIRTY_ANCHOR = "dirty_anchor"    # primary checkout dirty; refused to clobber
    ERROR = "error"                  # unexpected git failure

    @property
    def landed(self) -> bool:
        """True when trunk now contains the branch (fresh merge or already in)."""
        return self in (Outcome.LANDED, Outcome.ALREADY_LANDED)

    @property
    def should_block(self) -> bool:
        """True when the caller should block the task and preserve its branch."""
        return self in (Outcome.CONFLICT, Outcome.TESTS_FAILED)


@dataclass
class IntegrationResult:
    """Structured result of :func:`integrate_branch`."""

    outcome: Outcome
    branch: str
    trunk: str
    merged_sha: Optional[str] = None
    test_returncode: Optional[int] = None
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        parts = [f"{self.outcome.value} {self.branch} -> {self.trunk}"]
        if self.merged_sha:
            parts.append(f"@ {self.merged_sha[:12]}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


def _git(
    repo_root: Path, args: Sequence[str], *, timeout: int = 120
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _branch_exists(repo_root: Path, ref: str) -> bool:
    return _git(
        repo_root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{ref}"]
    ).returncode == 0


def _current_branch(repo_root: Path) -> Optional[str]:
    res = _git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if res.returncode == 0:
        return res.stdout.strip() or None
    return None  # detached HEAD


def _rev(repo_root: Path, ref: str) -> Optional[str]:
    res = _git(repo_root, ["rev-parse", "--verify", "--quiet", ref])
    return res.stdout.strip() if res.returncode == 0 else None


def _is_dirty(repo_root: Path) -> bool:
    res = _git(repo_root, ["status", "--porcelain", "--untracked-files=no"])
    return bool(res.stdout.strip())


def _git_common_dir(repo_root: Path) -> Path:
    """Absolute path to the repo's shared git dir (the main ``.git``).

    Same for the main worktree and every linked worktree, so it is the natural
    home for a per-repo lock. Falls back to ``<repo_root>/.git`` if git can't
    answer (e.g. not a repo — the caller will fail elsewhere).
    """
    res = _git(
        repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if res.returncode == 0 and res.stdout.strip():
        return Path(res.stdout.strip())
    return Path(repo_root) / ".git"


def main_worktree_root(path: Path) -> Optional[Path]:
    """Resolve the MAIN working-tree root for any path inside a repo/worktree.

    Landings must merge on the main checkout (where trunk lives), never inside a
    task's linked worktree. From a linked worktree, ``git rev-parse --show-toplevel``
    returns the *worktree* root, so we derive the main root from the shared git
    dir instead (``<main>/.git`` → its parent).
    """
    common = _git_common_dir(path)
    if common.name == ".git":
        return common.parent
    # Bare or unusual layout: best effort — the common dir's parent.
    return common.parent if common.exists() else None


def resolve_trunk_ref(
    repo_root: Path, candidates: Sequence[str] = DEFAULT_TRUNK_CANDIDATES
) -> Optional[str]:
    """Return the first existing local trunk branch, or ``None``.

    Purely local — the merge-queue trunk is the local ``main``; task branches
    are never pushed, so there is nothing to fetch. Falls through the candidate
    list (``main`` then ``master``) and returns the first that resolves.
    """
    for name in candidates:
        if _branch_exists(repo_root, name):
            return name
    return None


class TrunkLock:
    """Advisory file lock serializing all landings for one repo.

    ``fcntl.flock`` on a lock file under the repo. Blocking with a timeout so a
    second integrator waits for the first to finish rather than racing on the
    trunk ref. POSIX only (the deployment target); the lock is released on
    context exit and, by the OS, on process death — a crashed integrator never
    wedges the queue.
    """

    def __init__(self, repo_root: Path, *, timeout: float = 600.0, poll: float = 0.25):
        # Anchor the lock at the shared git common dir so every worktree of the
        # same repo serializes on ONE lock — and so it works when a linked
        # worktree's ``.git`` is a file, not a directory.
        self.lock_path = _git_common_dir(repo_root) / "hermes-trunk-integrate.lock"
        self.timeout = timeout
        self.poll = poll
        self._handle = None

    def __enter__(self) -> "TrunkLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise TimeoutError(
                        f"trunk integration lock busy after {self.timeout}s: "
                        f"{self.lock_path}"
                    )
                time.sleep(self.poll)

    def __exit__(self, *exc) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def integrate_branch(
    repo_root: Path,
    branch: str,
    *,
    trunk: Optional[str] = None,
    test_cmd: Optional[Sequence[str]] = None,
    test_timeout: int = 3600,
    run_tests: bool = True,
) -> IntegrationResult:
    """Merge ``branch`` into ``trunk`` on the primary checkout, gated by tests.

    Steps (all under the caller's :class:`TrunkLock`):

    1. Resolve trunk; bail if branch/trunk missing.
    2. If ``branch`` is already an ancestor of trunk (merged, or behind/equal)
       → ``ALREADY_LANDED`` (idempotent, non-destructive).
    3. Refuse a dirty primary checkout (``DIRTY_ANCHOR``) rather than clobber
       uncommitted work.
    4. Check out trunk, ``git merge --no-ff``. Conflict → abort, restore the
       original branch, ``CONFLICT``.
    5. Run the landing-gate suite on the merged tree. Red → hard-reset trunk to
       its pre-merge tip, restore the original branch, ``TESTS_FAILED``.
    6. Green → leave the primary on the advanced trunk → ``LANDED``.

    On every non-landing outcome the primary checkout is restored to exactly
    where it started, so a failed integration is invisible and non-destructive.
    """
    repo_root = Path(repo_root)
    trunk = trunk or resolve_trunk_ref(repo_root)
    if not trunk:
        return IntegrationResult(
            Outcome.ERROR, branch, trunk or "", detail="no trunk branch (main/master)"
        )
    if not _branch_exists(repo_root, branch):
        return IntegrationResult(Outcome.NO_BRANCH, branch, trunk)
    if not _branch_exists(repo_root, trunk):
        return IntegrationResult(
            Outcome.ERROR, branch, trunk, detail=f"trunk {trunk!r} does not exist"
        )

    # Idempotency: branch already in trunk (merged, or behind/equal) → no-op.
    if _git(repo_root, ["merge-base", "--is-ancestor", branch, trunk]).returncode == 0:
        return IntegrationResult(Outcome.ALREADY_LANDED, branch, trunk)

    if _is_dirty(repo_root):
        return IntegrationResult(
            Outcome.DIRTY_ANCHOR, branch, trunk,
            detail="anchor checkout has uncommitted changes; refusing to land",
        )

    orig_branch = _current_branch(repo_root)
    trunk_before = _rev(repo_root, trunk)

    def _restore() -> None:
        # Undo any merge left on trunk, then return to where we started.
        if trunk_before:
            _git(repo_root, ["checkout", "--quiet", trunk])
            _git(repo_root, ["reset", "--hard", "--quiet", trunk_before])
        if orig_branch and orig_branch != trunk:
            _git(repo_root, ["checkout", "--quiet", orig_branch])

    co = _git(repo_root, ["checkout", "--quiet", trunk])
    if co.returncode != 0:
        return IntegrationResult(
            Outcome.ERROR, branch, trunk,
            detail=f"could not checkout trunk: {(co.stderr or co.stdout).strip()}",
        )

    merge = _git(
        repo_root,
        ["merge", "--no-ff", "--no-edit", "-m",
         f"Merge branch '{branch}' into {trunk}", branch],
    )
    if merge.returncode != 0:
        _git(repo_root, ["merge", "--abort"])
        _restore()
        return IntegrationResult(
            Outcome.CONFLICT, branch, trunk,
            detail=(merge.stdout or merge.stderr).strip()[:500],
        )

    merged_sha = _rev(repo_root, "HEAD")

    if run_tests:
        cmd = list(test_cmd) if test_cmd is not None else list(DEFAULT_TEST_CMD)
        try:
            test_res = subprocess.run(
                cmd, cwd=str(repo_root), capture_output=True, text=True,
                timeout=test_timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            _restore()
            return IntegrationResult(
                Outcome.TESTS_FAILED, branch, trunk, merged_sha=merged_sha,
                detail=f"landing-gate tests timed out after {test_timeout}s",
            )
        if test_res.returncode != 0:
            tail = (test_res.stdout or "")[-800:] + (test_res.stderr or "")[-400:]
            _restore()
            return IntegrationResult(
                Outcome.TESTS_FAILED, branch, trunk, merged_sha=merged_sha,
                test_returncode=test_res.returncode, detail=tail.strip()[-800:],
            )

    _log.info("trunk-integrator: landed %s into %s @ %s", branch, trunk, merged_sha)
    return IntegrationResult(Outcome.LANDED, branch, trunk, merged_sha=merged_sha)
