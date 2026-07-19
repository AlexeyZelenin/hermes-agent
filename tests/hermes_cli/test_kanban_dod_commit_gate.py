"""DoD commit gate: a code task cannot reach ``done`` with uncommitted work.

Precedent (t_91f91fe8): a worker ran tests, called ``kanban_complete``, and went
to ``done`` leaving its whole repo tree dirty (uncommitted files + new tests).
The gate makes that state unreachable deterministically instead of relying on a
prompt reminder:

  * a dirty git workspace refuses completion (``dirty_tree``),
  * a run that landed no commit refuses completion (``no_commit``),
  * ``allow_dirty="<reason>"`` is the audited escape hatch,
  * research / domain / doc categories are exempt,
  * a shared checkout at the repo root is left to the post-completion janitor
    (unattributable — never hard-blocked).

Own throwaway repo + isolated HERMES_HOME per test; no host state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (no host board touched)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr or res.stdout}")
    return res


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "kanban@example.com")
    _git(repo, "config", "user.name", "Kanban Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def _make_repo_task(conn, repo: Path, *, kind: str = "worktree", path: Path | None = None):
    """Create + claim a repo-backed task; return its id (status running)."""
    tid = kb.create_task(
        conn,
        title="ship code",
        workspace_kind=kind,
        workspace_path=str(path or repo),
        created_by="test",
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return tid


def _set_category(conn, tid: str, category: str) -> None:
    # Bypass the catalog-validation in set_task_category — the gate reads the
    # raw column, and we only need the value present for this test.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET category = ? WHERE id = ?", (category, tid))


# ---------------------------------------------------------------------------
# 1) dirty tree → refusal
# ---------------------------------------------------------------------------

def test_dirty_tree_refuses_completion(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        tid = _make_repo_task(conn, repo)
        # Worker leaves an uncommitted file behind (the precedent).
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

        with pytest.raises(kb.UncommittedWorkError) as ei:
            kb.complete_task(conn, tid, summary="done, tests green")

        assert ei.value.kind == "dirty_tree"
        # Task stays in-flight — no state change on refusal.
        assert kb.get_task(conn, tid).status == "running"
        # Refusal is auditable + worker-facing.
        kinds = {e.kind for e in kb.list_events(conn, tid)}
        assert "completion_blocked_dirty_tree" in kinds
        assert any(c.author == "dod-gate" for c in kb.list_comments(conn, tid))


# ---------------------------------------------------------------------------
# 2) clean tree + a commit landed → passes
# ---------------------------------------------------------------------------

def test_clean_tree_with_commit_completes(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        tid = _make_repo_task(conn, repo)
        # Worker does the work AND commits it — the correct path.
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: add feature")

        assert kb.complete_task(conn, tid, summary="done + committed") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# 3) research category → passes without a commit
# ---------------------------------------------------------------------------

def test_research_category_completes_without_commit(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        tid = _make_repo_task(conn, repo)
        _set_category(conn, tid, "research")
        # Even a dirty tree is fine for an exempt category.
        (repo / "notes.md").write_text("findings\n", encoding="utf-8")

        assert kb.complete_task(conn, tid, summary="research findings") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# no-commit half (spec point 2): clean tree, run added no commit → refusal
# ---------------------------------------------------------------------------

def test_no_commit_refuses_completion(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        tid = _make_repo_task(conn, repo)  # base_commit = initial commit
        # Clean tree, but the run committed nothing over its base.
        with pytest.raises(kb.UncommittedWorkError) as ei:
            kb.complete_task(conn, tid, summary="nothing committed")

        assert ei.value.kind == "no_commit"
        assert kb.get_task(conn, tid).status == "running"
        kinds = {e.kind for e in kb.list_events(conn, tid)}
        assert "completion_blocked_no_commit" in kinds


# ---------------------------------------------------------------------------
# allow_dirty escape hatch → completes, override recorded
# ---------------------------------------------------------------------------

def test_allow_dirty_override_completes_and_audits(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        tid = _make_repo_task(conn, repo)
        (repo / "scratch.txt").write_text("wip\n", encoding="utf-8")

        assert kb.complete_task(
            conn, tid, summary="left dirty on purpose",
            allow_dirty=True, allow_dirty_reason="generated fixtures, keep uncommitted",
        ) is True
        assert kb.get_task(conn, tid).status == "done"
        override = [e for e in kb.list_events(conn, tid)
                    if e.kind == "completion_dod_override"]
        assert override, "override must be recorded for audit"
        assert override[0].payload["reason"] == "generated fixtures, keep uncommitted"


# ---------------------------------------------------------------------------
# scope guard: a shared dir checkout at the repo root is NOT hard-blocked
# ---------------------------------------------------------------------------

def test_shared_dir_checkout_is_not_blocked(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with kb.connect() as conn:
        # kind='dir' with the workspace == repo root == the shared anchor case:
        # other workers' changes are indistinguishable, so the gate defers to
        # the post-completion janitor instead of blocking.
        tid = _make_repo_task(conn, repo, kind="dir", path=repo)
        (repo / "someone_elses.py").write_text("y = 2\n", encoding="utf-8")

        assert kb.complete_task(conn, tid, summary="shared checkout") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# scope guard: a non-repo (scratch) workspace is inert (fails open)
# ---------------------------------------------------------------------------

def test_scratch_workspace_is_inert(kanban_home, tmp_path):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scratch task", created_by="test")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="no repo here") is True
        assert kb.get_task(conn, tid).status == "done"
