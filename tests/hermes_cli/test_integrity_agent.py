"""Tests for hermes_cli.integrity_agent — three-way integrity reconciliation.

The pure detectors are exercised with plain dicts (no board, git, or ledger);
the git probe runs against a throwaway temp repo; the findings store uses an
isolated temp zeus.db. Nothing here touches a live board or the real ledger.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import integrity_agent as ia


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "tests/hermes_cli/test_foo.py",
    "hermes_cli/test_foo.py",
    "pkg/foo_test.go",
    "web/app.test.ts",
    "web/app.spec.tsx",
    "spec/models/user_spec.rb",
])
def test_is_test_path_positive(path):
    assert ia.is_test_path(path)


@pytest.mark.parametrize("path", [
    "hermes_cli/integrity_agent.py",
    "docs/design.md",
    "web/app.ts",
])
def test_is_test_path_negative(path):
    assert not ia.is_test_path(path)


def test_is_code_path_excludes_tests_and_docs():
    assert ia.is_code_path("hermes_cli/integrity_agent.py")
    assert not ia.is_code_path("tests/test_x.py")   # a test is not product code
    assert not ia.is_code_path("docs/readme.md")     # docs are not code


# ---------------------------------------------------------------------------
# expects_delivery
# ---------------------------------------------------------------------------


def test_expects_delivery_branch_or_project_or_repo():
    assert ia.expects_delivery({"branch_name": "wt/t_1"})
    assert ia.expects_delivery({"project_id": "p1"})
    assert ia.expects_delivery({"workspace_kind": "worktree"})
    # A pure scratch card with no anchor is left to the Langfuse phase.
    assert not ia.expects_delivery({"workspace_kind": "scratch"})
    assert not ia.expects_delivery({})


# ---------------------------------------------------------------------------
# Drift (a): scratch-trap
# ---------------------------------------------------------------------------


def test_scratch_trap_fires_when_branch_card_has_no_commit():
    task = {"id": "t_1", "title": "Feature", "branch_name": "wt/t_1"}
    delivery = {"delivered": False, "files": []}
    f = ia.detect_scratch_trap(task, delivery)
    assert f is not None
    assert f["kind"] == ia.KIND_SCRATCH_TRAP
    assert f["finding_key"] == "integrity:t_1:scratch_trap"
    assert f["severity"] == "warning"


def test_scratch_trap_silent_when_delivered():
    task = {"id": "t_1", "title": "Feature", "branch_name": "wt/t_1"}
    delivery = {"delivered": True, "files": ["a.py"]}
    assert ia.detect_scratch_trap(task, delivery) is None


def test_scratch_trap_silent_for_unanchored_scratch_card():
    task = {"id": "t_1", "title": "Chore", "workspace_kind": "scratch"}
    delivery = {"delivered": False, "files": []}
    assert ia.detect_scratch_trap(task, delivery) is None


# ---------------------------------------------------------------------------
# Drift (b): lost-delivery (deferred — active only with a trace)
# ---------------------------------------------------------------------------


def test_lost_delivery_none_without_trace():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["a.py"]}
    assert ia.detect_lost_delivery(task, delivery, None) is None


def test_lost_delivery_fires_for_created_file_absent_now():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["a.py"]}
    trace = {"created_files": ["a.py", "ghost.py"]}
    f = ia.detect_lost_delivery(task, delivery, trace)
    assert f is not None
    assert f["kind"] == ia.KIND_LOST_DELIVERY
    assert "ghost.py" in f["evidence"]["lost_files"]
    assert "a.py" not in f["evidence"]["lost_files"]  # a.py landed


def test_lost_delivery_respects_present_files_from_probe():
    task = {"id": "t_1", "title": "F"}
    # ghost.py isn't in the delivery commits but is present on disk (moved).
    delivery = {"delivered": True, "files": ["a.py"], "present_files": ["ghost.py"]}
    trace = {"created_files": ["a.py", "ghost.py"]}
    assert ia.detect_lost_delivery(task, delivery, trace) is None


# ---------------------------------------------------------------------------
# Drift (c): coverage-gap (static)
# ---------------------------------------------------------------------------


def test_coverage_gap_fires_for_code_without_test():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["hermes_cli/x.py", "hermes_cli/y.py"]}
    f = ia.detect_coverage_gap(task, delivery)
    assert f is not None
    assert f["kind"] == ia.KIND_COVERAGE_GAP
    assert f["severity"] == "info"  # no DoD facet -> soft


def test_coverage_gap_silent_when_test_present():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["hermes_cli/x.py", "tests/test_x.py"]}
    assert ia.detect_coverage_gap(task, delivery) is None


def test_coverage_gap_silent_for_docs_only_delivery():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["docs/design.md", "README.md"]}
    assert ia.detect_coverage_gap(task, delivery) is None


def test_coverage_gap_warning_when_dod_requires_tests():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["hermes_cli/x.py"]}
    f = ia.detect_coverage_gap(task, delivery, dod={"requires_tests": True})
    assert f is not None and f["severity"] == "warning"


def test_coverage_gap_suppressed_when_dod_says_no_tests_needed():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["hermes_cli/x.py"]}
    assert ia.detect_coverage_gap(task, delivery, dod={"requires_tests": False}) is None


def test_coverage_gap_silent_when_not_delivered():
    task = {"id": "t_1", "title": "F"}
    assert ia.detect_coverage_gap(task, {"delivered": False, "files": []}) is None


# ---------------------------------------------------------------------------
# Drift (d): unmerged-branch (stranded delivery off trunk)
# ---------------------------------------------------------------------------


def test_unmerged_branch_fires_for_commits_off_trunk():
    task = {"id": "t_a483b17b", "title": "zeus_watchdog"}
    delivery = {
        "delivered": True, "files": ["zeus_watchdog/x.py"], "trunk": "main",
        "unmerged_commits": ["64948e0deadbeef"],
        "unmerged_branches": ["ra/t_a483b17b"],
    }
    f = ia.detect_unmerged_branch(task, delivery)
    assert f is not None
    assert f["kind"] == ia.KIND_UNMERGED_BRANCH
    assert f["finding_key"] == "integrity:t_a483b17b:unmerged_branch"
    assert f["severity"] == "warning"
    assert "ra/t_a483b17b" in f["detail"]
    assert f["evidence"]["unmerged_commits"] == ["64948e0deadbeef"]


def test_unmerged_branch_silent_when_all_commits_landed():
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["a.py"], "trunk": "main",
                "unmerged_commits": [], "unmerged_branches": []}
    assert ia.detect_unmerged_branch(task, delivery) is None


def test_unmerged_branch_silent_when_no_trunk_resolved():
    # Probe left unmerged_commits empty (no main/master) -> no false positive.
    task = {"id": "t_1", "title": "F"}
    delivery = {"delivered": True, "files": ["a.py"], "trunk": None,
                "unmerged_commits": []}
    assert ia.detect_unmerged_branch(task, delivery) is None


def test_unmerged_branch_disjoint_from_scratch_trap():
    # A stranded delivery is delivered=True, so scratch-trap stays silent and
    # only the unmerged-branch drift fires — the two never double-report.
    task = {"id": "t_x", "title": "F", "branch_name": "ra/t_x"}
    delivery = {"delivered": True, "files": ["x.py"], "trunk": "main",
                "unmerged_commits": ["abc123"], "unmerged_branches": ["ra/t_x"]}
    assert ia.detect_scratch_trap(task, delivery) is None
    assert ia.detect_unmerged_branch(task, delivery) is not None


# ---------------------------------------------------------------------------
# reconcile (pure orchestration)
# ---------------------------------------------------------------------------


def test_reconcile_combines_detectors_and_skips_probe_errors():
    tasks = [
        {"id": "t_trap", "title": "A", "branch_name": "wt/a"},
        {"id": "t_gap", "title": "B", "branch_name": "wt/b"},
        {"id": "t_ok", "title": "C", "branch_name": "wt/c"},
        {"id": "", "title": "no id"},  # skipped
    ]

    def git_probe(tid):
        if tid == "t_trap":
            return {"delivered": False, "files": []}
        if tid == "t_gap":
            return {"delivered": True, "files": ["x.py"]}
        if tid == "t_ok":
            return {"delivered": True, "files": ["x.py", "tests/test_x.py"]}
        raise RuntimeError("unexpected probe call")

    findings = ia.reconcile(tasks, git_probe)
    kinds = {(f["task_id"], f["kind"]) for f in findings}
    assert ("t_trap", ia.KIND_SCRATCH_TRAP) in kinds
    assert ("t_gap", ia.KIND_COVERAGE_GAP) in kinds
    assert not any(f["task_id"] == "t_ok" for f in findings)


def test_reconcile_git_probe_failure_degrades_to_no_delivery():
    tasks = [{"id": "t_1", "title": "A", "branch_name": "wt/a"}]

    def boom(_tid):
        raise OSError("git exploded")

    findings = ia.reconcile(tasks, boom)
    # No delivery evidence -> scratch-trap, not a crash.
    assert [f["kind"] for f in findings] == [ia.KIND_SCRATCH_TRAP]


# ---------------------------------------------------------------------------
# Findings store: emit / clear / scan_and_emit
# ---------------------------------------------------------------------------


def _open_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def test_emit_and_clear_roundtrip(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    task = {"id": "t_1", "title": "F", "branch_name": "wt/1"}
    finding = ia.detect_scratch_trap(task, {"delivered": False, "files": []})
    ia.emit_finding(conn, finding, board="ra", now=1000.0)

    row = conn.execute(
        "SELECT * FROM findings WHERE source='integrity'").fetchone()
    assert row["finding_key"] == "integrity:t_1:scratch_trap"
    assert row["status"] == "open"
    assert row["board"] == "ra"

    ia.clear_finding(conn, board="ra", finding_key=finding["finding_key"], now=2000.0)
    row = conn.execute(
        "SELECT status FROM findings WHERE finding_key=?",
        (finding["finding_key"],)).fetchone()
    assert row["status"] == "obsolete"
    conn.close()


def test_emit_preserves_human_dismissal(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    task = {"id": "t_1", "title": "F", "branch_name": "wt/1"}
    finding = ia.detect_scratch_trap(task, {"delivered": False, "files": []})
    ia.emit_finding(conn, finding, board="ra", now=1000.0)
    conn.execute("UPDATE findings SET status='dismissed' WHERE finding_key=?",
                 (finding["finding_key"],))
    conn.commit()
    # Re-emitting must not resurrect a human-dismissed finding.
    ia.emit_finding(conn, finding, board="ra", now=1500.0)
    row = conn.execute("SELECT status FROM findings WHERE finding_key=?",
                       (finding["finding_key"],)).fetchone()
    assert row["status"] == "dismissed"
    conn.close()


def test_scan_and_emit_clears_recovered_kinds(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    task = {"id": "t_1", "title": "F", "branch_name": "wt/1"}
    trap = ia.detect_scratch_trap(task, {"delivered": False, "files": []})
    ia.scan_and_emit([task], [trap], conn, board="ra", now=1000.0)
    assert conn.execute(
        "SELECT status FROM findings WHERE finding_key='integrity:t_1:scratch_trap'"
    ).fetchone()["status"] == "open"

    # Next pass: the card now delivered -> no findings -> the open one clears.
    ia.scan_and_emit([task], [], conn, board="ra", now=2000.0)
    assert conn.execute(
        "SELECT status FROM findings WHERE finding_key='integrity:t_1:scratch_trap'"
    ).fetchone()["status"] == "obsolete"
    conn.close()


def test_scan_and_emit_noop_without_conn():
    task = {"id": "t_1", "title": "F", "branch_name": "wt/1"}
    trap = ia.detect_scratch_trap(task, {"delivered": False, "files": []})
    assert ia.scan_and_emit([task], [trap], None, board="ra") == []


# ---------------------------------------------------------------------------
# Real git probe against a throwaway repo
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def temp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: base")
    return repo


def test_delivery_for_task_finds_commit_and_files(temp_repo):
    (temp_repo / "feature.py").write_text("x = 1\n")
    (temp_repo / "test_feature.py").write_text("def test_x(): pass\n")
    _git(temp_repo, "add", "-A")
    _git(temp_repo, "commit", "-q", "-m", "feat: shiny thing (t_abc12345)")

    d = ia.delivery_for_task(temp_repo, "t_abc12345")
    assert d["delivered"] is True
    assert "feature.py" in d["files"]
    assert "test_feature.py" in d["files"]


def test_delivery_for_task_absent_for_unknown_id(temp_repo):
    d = ia.delivery_for_task(temp_repo, "t_notreal0")
    assert d["delivered"] is False
    assert d["files"] == []


@pytest.fixture()
def trunk_repo(tmp_path):
    """A temp repo whose default branch is a real trunk (``main``)."""
    repo = tmp_path / "trunk_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: base")
    return repo


def test_delivery_for_task_on_unmerged_branch(temp_repo):
    _git(temp_repo, "checkout", "-q", "-b", "wt/t_branch01")
    (temp_repo / "b.py").write_text("y = 2\n")
    _git(temp_repo, "add", "-A")
    _git(temp_repo, "commit", "-q", "-m", "feat: branch work (t_branch01)")
    _git(temp_repo, "checkout", "-q", "-")  # back to default branch
    # --all reach finds it even though it's not merged.
    d = ia.delivery_for_task(temp_repo, "t_branch01")
    assert d["delivered"] is True
    assert "b.py" in d["files"]


def test_delivery_reports_unmerged_commit_and_branch(trunk_repo):
    # A commit stranded on a task branch, never merged into main.
    _git(trunk_repo, "checkout", "-q", "-b", "ra/t_stranded")
    (trunk_repo / "watchdog.py").write_text("z = 3\n")
    _git(trunk_repo, "add", "-A")
    _git(trunk_repo, "commit", "-q", "-m", "feat: watchdog (t_stranded)")
    _git(trunk_repo, "checkout", "-q", "main")

    d = ia.delivery_for_task(trunk_repo, "t_stranded")
    assert d["delivered"] is True
    assert d["trunk"] == "main"
    assert len(d["unmerged_commits"]) == 1
    assert d["unmerged_branches"] == ["ra/t_stranded"]


def test_delivery_no_unmerged_after_merge_to_trunk(trunk_repo):
    _git(trunk_repo, "checkout", "-q", "-b", "ra/t_landed")
    (trunk_repo / "landed.py").write_text("z = 4\n")
    _git(trunk_repo, "add", "-A")
    _git(trunk_repo, "commit", "-q", "-m", "feat: landed work (t_landed)")
    _git(trunk_repo, "checkout", "-q", "main")
    _git(trunk_repo, "merge", "-q", "--no-ff", "--no-edit", "ra/t_landed")

    d = ia.delivery_for_task(trunk_repo, "t_landed")
    assert d["delivered"] is True
    assert d["unmerged_commits"] == []
    assert d["unmerged_branches"] == []


def test_reconcile_end_to_end_flags_unmerged_branch(trunk_repo):
    _git(trunk_repo, "checkout", "-q", "-b", "ra/t_leak001")
    (trunk_repo / "leak.py").write_text("z = 5\n")
    _git(trunk_repo, "add", "-A")
    _git(trunk_repo, "commit", "-q", "-m", "feat: leak (t_leak001)")
    _git(trunk_repo, "checkout", "-q", "main")

    tasks = [{"id": "t_leak001", "title": "Leak", "branch_name": "ra/t_leak001"}]
    findings = ia.reconcile(tasks, ia.default_git_probe(trunk_repo))
    kinds = [f["kind"] for f in findings]
    assert ia.KIND_UNMERGED_BRANCH in kinds
    assert ia.KIND_SCRATCH_TRAP not in kinds  # it DID deliver, just not to trunk


def test_default_git_probe_end_to_end_scratch_trap(temp_repo):
    # A done, branch-anchored card whose work never got committed.
    tasks = [{"id": "t_lost001", "title": "Lost", "branch_name": "wt/t_lost001"}]
    findings = ia.reconcile(tasks, ia.default_git_probe(temp_repo))
    assert [f["kind"] for f in findings] == [ia.KIND_SCRATCH_TRAP]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_no_emit_clean_repo_returns_zero(temp_repo, capsys):
    # No done tasks (isolated HERMES_HOME via the autouse hermetic fixture) ->
    # nothing to reconcile -> clean exit.
    rc = ia.main(["--repo", str(temp_repo), "--no-emit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "дрейфа не найдено" in out


def test_main_json_output(temp_repo, capsys):
    rc = ia.main(["--repo", str(temp_repo), "--no-emit", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"finding_count": 0' in out
