"""Per-task git-worktree isolation for a whole board (t_dad8773c).

Without this, every card on a board lands in a scratch cwd while the worker
edits the shared live checkout by absolute path — two parallel workers on the
same repo overwrite each other, which is why ``agent_limit`` had to stay at 1.
Binding a project to the board gives each task its own worktree, in EVERY repo
the project spans, on one deterministic branch.

Each test builds throwaway git repos plus a throwaway kanban home + projects.db
under ``tmp_path`` (the shared conftest already pins ``HERMES_HOME``). No host
repo, no real board, no real projects store is touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_cli.trunk_integrator import Outcome


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str, check: bool = True):
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr or res.stdout}")
    return res


def _repo(tmp_path: Path, name: str) -> Path:
    r = tmp_path / name
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    _git(r, "checkout", "-q", "-b", "main")
    (r / "README.md").write_text(f"{name}\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _board_project(board: str, folders: list[Path], **kw) -> pdb.Project:
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(
            pc, name=kw.pop("name", "Roul"), board_slug=board,
            folders=[str(f) for f in folders], **kw,
        )
        return pdb.get_project(pc, pid)


# ---------------------------------------------------------------------------
# projects_db.project_for_board
# ---------------------------------------------------------------------------


def test_project_for_board_resolves_the_bound_project(kanban_home, tmp_path):
    proj = _board_project("roul", [_repo(tmp_path, "engine")])
    with pdb.connect_closing() as pc:
        assert pdb.project_for_board(pc, "roul").id == proj.id
        assert pdb.project_for_board(pc, "other-board") is None


def test_project_for_board_ignores_archived_and_repo_less_projects(kanban_home, tmp_path):
    """A binding only counts when there is a repo to anchor a worktree in."""
    with pdb.connect_closing() as pc:
        pdb.create_project(pc, name="No repo", board_slug="roul")
        archived = pdb.create_project(
            pc, name="Old", board_slug="roul", folders=[str(_repo(tmp_path, "old"))]
        )
        pdb.archive_project(pc, archived)
        assert pdb.project_for_board(pc, "roul") is None


# ---------------------------------------------------------------------------
# create_task: board binding -> worktree task
# ---------------------------------------------------------------------------


def test_board_bound_project_turns_plain_tasks_into_worktree_tasks(kanban_home, tmp_path):
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul")
    proj = _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="Fix dispatch", board="roul")
        task = kb.get_task(conn, tid)

    # No project_id was passed — the board binding supplied it.
    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(engine / ".worktrees" / tid)
    assert task.branch_name == f"{proj.slug}/{tid}-fix-dispatch"
    # The project's OTHER repo is frozen onto the task for the dispatcher.
    assert task.linked_repos == [str(plugin.resolve())]


def test_explicit_workspace_path_is_left_alone(kanban_home, tmp_path):
    """An operator who named a checkout keeps it — no silent re-anchoring."""
    kb.create_board("roul")
    _board_project("roul", [_repo(tmp_path, "engine")])
    elsewhere = tmp_path / "elsewhere"

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(
            conn, title="x", board="roul",
            workspace_kind="dir", workspace_path=str(elsewhere),
        )
        task = kb.get_task(conn, tid)

    assert task.workspace_kind == "dir"
    assert task.project_id is None
    assert task.linked_repos is None


def test_unbound_board_keeps_scratch_tasks(kanban_home, tmp_path):
    kb.create_board("solo")
    with kb.connect_closing(board="solo") as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="x", board="solo"))
    assert task.workspace_kind == "scratch"
    assert task.linked_repos is None


def test_single_repo_project_freezes_no_linked_repos(kanban_home, tmp_path):
    kb.create_board("roul")
    _board_project("roul", [_repo(tmp_path, "engine")])
    with kb.connect_closing(board="roul") as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="x", board="roul"))
    assert task.workspace_kind == "worktree"
    assert task.linked_repos is None


def test_non_git_project_folder_is_not_a_linked_repo(kanban_home, tmp_path):
    """Docs/asset folders have nothing to branch — they must not be worktree'd."""
    engine = _repo(tmp_path, "engine")
    notes = tmp_path / "notes"
    notes.mkdir()
    kb.create_board("roul")
    _board_project("roul", [engine, notes])

    with kb.connect_closing(board="roul") as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="x", board="roul"))
    assert task.linked_repos is None


# ---------------------------------------------------------------------------
# resolve_workspace: one worktree per repo, one branch
# ---------------------------------------------------------------------------


def test_resolve_workspace_materializes_every_repo_on_one_branch(kanban_home, tmp_path):
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul")
    _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="two repos", board="roul")
        task = kb.get_task(conn, tid)
        cwd = kb.resolve_workspace(task, board="roul")

    engine_wt = engine / ".worktrees" / tid
    plugin_wt = plugin / ".worktrees" / tid
    assert cwd == engine_wt.resolve()
    assert (engine_wt / "README.md").read_text() == "engine\n"
    assert (plugin_wt / "README.md").read_text() == "plugin\n"
    # Same branch in both, so the pair lands as one unit.
    for wt in (engine_wt, plugin_wt):
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == (
            task.branch_name
        )
    # And the live checkouts stay on trunk — untouched by the task.
    for repo in (engine, plugin):
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"


def test_dispatch_path_materializes_the_linked_repos_too(kanban_home, tmp_path):
    """The spawn loop calls ``_resolve_worktree_workspace`` directly, NOT
    ``resolve_workspace`` — the extra repos must be created there as well or a
    two-repo card silently gets one checkout and edits the other's live tree."""
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul")
    _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="two repos", board="roul")
        task = kb.get_task(conn, tid)
        path, branch = kb._resolve_worktree_workspace(task, board="roul")

    assert path == (engine / ".worktrees" / tid).resolve()
    assert (plugin / ".worktrees" / tid / "README.md").exists()
    assert _git(plugin / ".worktrees" / tid, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch


def test_missing_linked_repo_fails_loudly(kanban_home, tmp_path):
    """Silently skipping a repo would push the worker back onto the live tree."""
    engine = _repo(tmp_path, "engine")
    kb.create_board("roul")
    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(
            conn, title="x", board="roul",
            workspace_kind="worktree", workspace_path=str(engine),
            branch_name="roul/x",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET linked_repos=? WHERE id=?",
                (f'["{tmp_path / "not-a-repo"}"]', tid),
            )
        task = kb.get_task(conn, tid)
        with pytest.raises(ValueError, match="not a git repo"):
            kb.resolve_workspace(task, board="roul")


# ---------------------------------------------------------------------------
# worker prompt
# ---------------------------------------------------------------------------


def test_worker_context_names_every_worktree_and_bans_the_live_checkout(
    kanban_home, tmp_path
):
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul")
    _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="two repos", board="roul")
        ctx = kb.build_worker_context(conn, tid)

    assert str(engine / ".worktrees" / tid) in ctx
    assert str(plugin / ".worktrees" / tid) in ctx
    assert "OFF-LIMITS" in ctx
    assert "your cwd" in ctx


def test_worker_context_has_no_repo_section_for_scratch_tasks(kanban_home, tmp_path):
    kb.create_board("solo")
    with kb.connect_closing(board="solo") as conn:
        ctx = kb.build_worker_context(conn, kb.create_task(conn, title="x", board="solo"))
    assert "isolated per-task worktrees" not in ctx


# ---------------------------------------------------------------------------
# relinking a board that was already running
# ---------------------------------------------------------------------------


def test_relink_converts_the_unstarted_backlog_only(kanban_home, tmp_path):
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul")

    with kb.connect_closing(board="roul") as conn:
        queued = kb.create_task(conn, title="queued", board="roul", assignee="w")
        running = kb.create_task(conn, title="running", board="roul", assignee="w")
        kb.claim_task(conn, running)
        # Bind the project only AFTER the backlog exists — the retro-fit case.
        proj = _board_project("roul", [engine, plugin])
        assert kb.relink_board_tasks(conn, board="roul") == [queued]

        converted = kb.get_task(conn, queued)
        assert converted.workspace_kind == "worktree"
        assert converted.workspace_path == str(engine / ".worktrees" / queued)
        assert converted.branch_name == f"{proj.slug}/{queued}-queued"
        assert converted.linked_repos == [str(plugin.resolve())]
        # A started task keeps its materialized workspace — no work stranded.
        assert kb.get_task(conn, running).workspace_kind == "scratch"


def test_relink_is_a_noop_without_a_bound_project(kanban_home, tmp_path):
    kb.create_board("solo")
    with kb.connect_closing(board="solo") as conn:
        kb.create_task(conn, title="x", board="solo", assignee="w")
        assert kb.relink_board_tasks(conn, board="solo") == []


def test_relink_dry_run_changes_nothing(kanban_home, tmp_path):
    kb.create_board("roul")
    _board_project("roul", [_repo(tmp_path, "engine")])
    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="x", board="roul", assignee="w")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='scratch', workspace_path=NULL, "
                "project_id=NULL, branch_name=NULL WHERE id=?",
                (tid,),
            )
        assert kb.relink_board_tasks(conn, board="roul", dry_run=True) == [tid]
        assert kb.get_task(conn, tid).workspace_kind == "scratch"


# ---------------------------------------------------------------------------
# landing a multi-repo task
# ---------------------------------------------------------------------------


def _commit_in(worktree: Path, filename: str, text: str) -> None:
    (worktree / filename).write_text(text)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", f"work: {filename}")


def test_integrate_lands_the_branch_in_both_repos_and_prunes_both(kanban_home, tmp_path):
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul", default_workdir=str(engine))
    _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="two repos", board="roul")
        task = kb.get_task(conn, tid)
        branch = task.branch_name
        kb.resolve_workspace(task, board="roul")
        _commit_in(engine / ".worktrees" / tid, "a.txt", "alpha\n")
        _commit_in(plugin / ".worktrees" / tid, "b.txt", "beta\n")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        res = kb.integrate_task(conn, tid, board="roul", test_cmd=["true"])

    assert res.outcome is Outcome.LANDED
    assert (engine / "a.txt").read_text() == "alpha\n"
    assert (plugin / "b.txt").read_text() == "beta\n"
    for repo in (engine, plugin):
        assert not (repo / ".worktrees" / tid).exists()
        assert _git(repo, "rev-parse", "--verify", branch, check=False).returncode != 0


def test_integrate_stops_and_blocks_when_the_second_repo_conflicts(kanban_home, tmp_path):
    """Half-landing a two-repo card is worse than landing none of it."""
    engine = _repo(tmp_path, "engine")
    plugin = _repo(tmp_path, "plugin")
    kb.create_board("roul", default_workdir=str(engine))
    _board_project("roul", [engine, plugin])

    with kb.connect_closing(board="roul") as conn:
        tid = kb.create_task(conn, title="two repos", board="roul")
        task = kb.get_task(conn, tid)
        branch = task.branch_name
        kb.resolve_workspace(task, board="roul")
        _commit_in(engine / ".worktrees" / tid, "a.txt", "alpha\n")
        _commit_in(plugin / ".worktrees" / tid, "clash.txt", "from-branch\n")
        # Trunk in the SECOND repo moves under the branch, on the same file.
        (plugin / "clash.txt").write_text("from-trunk\n")
        _git(plugin, "add", "-A")
        _git(plugin, "commit", "-q", "-m", "trunk clash")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        res = kb.integrate_task(conn, tid, board="roul", test_cmd=["true"])
        assert kb.get_task(conn, tid).status == "blocked"

    assert res.outcome is Outcome.CONFLICT
    # The conflicting repo keeps the branch + worktree, so nothing is lost.
    assert (plugin / ".worktrees" / tid).exists()
    assert _git(plugin, "rev-parse", "--verify", branch, check=False).returncode == 0
