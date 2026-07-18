"""Integrity agent — three-way reconciliation kanban↔git↔Langfuse + coverage audit.

Operator model (task t_b3d55a65): a *regular* process (an extension of
reflection — see :mod:`hermes_cli.regular_crons`) that periodically checks the
project's INTEGRITY — the thing the night-shift operator did by hand: did a
"done" card's delivery actually land, or is it stuck in a scratch workspace; is
a soft-product feature backed by a test at all. It automates exactly that nanny
loop so the product absorbs the babysitter.

The design is a *three-way* reconciliation ("что с чем сверять"):

* **Заявлено (kanban)** — what a card claims: ``status='done'``.
* **Фактически (git)** — what is really in the repo now: does a commit
  referencing the task id exist, and what files did it touch.
* **Исторически (Langfuse)** — what the session actually did (tool calls,
  touched files) — the immutable trail. *Deferred* until the OTel seam lands
  (task t_795601eb); modelled here as an injectable probe that defaults to
  absent, so drift-class (b) simply doesn't fire yet.

Integrity = all three agree. DRIFT signals:

* (a) **scratch-trap** — card is done but there is no delivery in git (work
  stranded in a scratch workspace, never committed);
* (b) **lost-delivery** — Langfuse shows the session created file X, but X is
  absent now (moved/deleted, yet the trail proves it existed) — *deferred*;
* (c) **coverage-gap** — a code delivery shipped with no test alongside it (or,
  once the DoD facet of t_48c56a39 exists, a facet claims tests but none exist).

TEST COVERAGE is checked STATICALLY (no tenant, no board spin-up): we look at
whether the delivery commit *also* touched a test file, and — when a DoD-facet
probe is supplied — whether a facet demanding tests is satisfied. A real test
*run* is out of scope here; it happens only pointwise in the executor's existing
hermetic sandbox, never as a standing instance.

Findings land in the shared zeus ``findings`` store tagged ``source=integrity``
(the same store :mod:`hermes_cli.regular_crons` pushes to); only anomalies push,
the pull view stays quiet. The agent's own token spend is attributable via the
zeus ledger by its cron run-session prefix (see :mod:`hermes_cli.zeus_tokens`),
so no separate accounting lives here.

Pure, dependency-injectable: the detectors take plain dicts and probe callables
so they unit-test without a live board, git repo, or ledger; the
``default_git_probe`` / ``run_integrity_scan`` entry points wire the real repo
and zeus.db. Everything degrades to a bare, side-effect-free scan when the
ledger is absent.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

FINDINGS_SOURCE = "integrity"

# Drift kinds. Stable strings — they form part of the finding_key, so renaming
# one orphans its prior findings.
KIND_SCRATCH_TRAP = "scratch_trap"
KIND_LOST_DELIVERY = "lost_delivery"
KIND_COVERAGE_GAP = "coverage_gap"
ALL_KINDS = (KIND_SCRATCH_TRAP, KIND_LOST_DELIVERY, KIND_COVERAGE_GAP)

# A "GitDelivery" is a plain dict: {"delivered": bool, "commits": [sha, ...],
#   "files": [repo-relative path, ...]}. A "LangfuseTrace" (deferred) is
#   {"created_files": [...], "touched_files": [...]} or None. A "DodFacet"
#   (t_48c56a39, not yet implemented) is {"requires_tests": bool} or None.
GitProbe = Callable[[str], dict[str, Any]]
LangfuseProbe = Callable[[str], Optional[dict[str, Any]]]
DodProbe = Callable[[str], Optional[dict[str, Any]]]

# Path classification for the static coverage check. Test paths win over code
# paths (a file under tests/ is a test, never counted as shipped product code).
_CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
                  ".rb", ".cs", ".kt", ".swift", ".c", ".cc", ".cpp", ".h")


# --- Path classification (pure) ---------------------------------------------


def is_test_path(path: str) -> bool:
    """True if ``path`` looks like a test file (any language convention)."""
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or "_test." in base
        or ".test." in base
        or "_spec." in base
        or ".spec." in base
        or "/tests/" in p
        or p.startswith("tests/")
        or "/test/" in p
        or "/spec/" in p
    )


def is_code_path(path: str) -> bool:
    """True if ``path`` is product source code (a code suffix, not a test)."""
    if is_test_path(path):
        return False
    return path.lower().endswith(_CODE_SUFFIXES)


def _split_delivery(files: Iterable[str]) -> tuple[list[str], list[str]]:
    """Partition delivered paths into (code_files, test_files)."""
    code = [f for f in files if is_code_path(f)]
    tests = [f for f in files if is_test_path(f)]
    return code, tests


# --- Expectation predicate (pure) -------------------------------------------


def expects_delivery(task: dict[str, Any]) -> bool:
    """True if a done card was expected to leave a commit in the repo.

    Without the Langfuse trail we can't prove a *scratch* card touched code, so
    the git-only phase is deliberately conservative: it flags a missing delivery
    only when the dispatcher allocated a branch / worktree / project anchor for
    the card (i.e. it was set up to produce commits). Pure-scratch cards with no
    such anchor are left to the Langfuse phase (t_795601eb), which will prove
    file creation directly.
    """
    if task.get("branch_name") or task.get("project_id"):
        return True
    return str(task.get("workspace_kind") or "") in ("repo", "worktree")


# --- Drift detectors (pure) -------------------------------------------------


def detect_scratch_trap(
    task: dict[str, Any], delivery: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Drift (a): card done + expected a delivery + nothing in git."""
    if not expects_delivery(task) or delivery.get("delivered"):
        return None
    tid = str(task.get("id"))
    title = str(task.get("title") or tid)
    branch = task.get("branch_name")
    where = f" (ветка {branch})" if branch else ""
    return _finding(
        task, KIND_SCRATCH_TRAP,
        title=f"«{title}» помечена done, но деливери в git нет",
        detail=(
            f"Задача {tid} завершена, но ни один коммит не ссылается на неё"
            f"{where}. Похоже на scratch-ловушку: работа осталась в песочнице "
            "и не приземлилась в репозиторий."
        ),
        category="drift", severity="warning",
        evidence={"branch": branch, "commits": []},
    )


def detect_lost_delivery(
    task: dict[str, Any],
    delivery: dict[str, Any],
    trace: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Drift (b): Langfuse recorded created files that are absent in git now.

    Deferred until the OTel seam (t_795601eb) feeds a real trace; with
    ``trace is None`` this is a no-op. ``delivery["files"]`` is the set of paths
    the delivery commits touched — a created file that never appears there (and
    isn't on disk) is a lost delivery.
    """
    if not trace:
        return None
    created = [str(p) for p in (trace.get("created_files") or [])]
    if not created:
        return None
    landed = set(delivery.get("files") or [])
    present = set(delivery.get("present_files") or [])  # optional on-disk set from the probe
    lost = [p for p in created if p not in landed and p not in present]
    if not lost:
        return None
    tid = str(task.get("id"))
    title = str(task.get("title") or tid)
    return _finding(
        task, KIND_LOST_DELIVERY,
        title=f"«{title}»: Langfuse видит созданные файлы, а в репо их нет",
        detail=(
            f"След сессии задачи {tid} показывает создание файлов, которых "
            f"сейчас нет ни в коммитах, ни на диске: {', '.join(lost)}. "
            "Перенесли/удалили, а Langfuse помнит."
        ),
        category="drift", severity="warning",
        evidence={"lost_files": lost},
    )


def detect_coverage_gap(
    task: dict[str, Any],
    delivery: dict[str, Any],
    dod: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Drift (c): a code delivery shipped without a test alongside it.

    Static only: partitions the delivery's touched files into product code vs
    test files. A delivery that touched code but no test is a gap. When a DoD
    facet (t_48c56a39) is supplied and demands tests, the gap is a ``warning``;
    otherwise it's a soft ``info`` (docs/config-only deliveries touch no code
    and are skipped entirely).
    """
    if not delivery.get("delivered"):
        return None  # scratch-trap covers "nothing landed"
    code, tests = _split_delivery(delivery.get("files") or [])
    if not code or tests:
        return None
    requires = bool(dod and dod.get("requires_tests"))
    if dod and not dod.get("requires_tests"):
        return None  # facet explicitly says this card needs no test
    tid = str(task.get("id"))
    title = str(task.get("title") or tid)
    severity = "warning" if requires else "info"
    claim = " (DoD-фасет требует тесты)" if requires else ""
    return _finding(
        task, KIND_COVERAGE_GAP,
        title=f"«{title}»: код без теста{claim}",
        detail=(
            f"Деливери задачи {tid} тронул {len(code)} файл(ов) кода, но ни "
            f"одного тест-файла: {', '.join(code[:5])}"
            f"{' …' if len(code) > 5 else ''}. Покрытие не заявлено статически."
        ),
        category="coverage", severity=severity,
        evidence={"code_files": code, "requires_tests": requires},
    )


def _finding(task: dict[str, Any], kind: str, *, title: str, detail: str,
             category: str, severity: str, evidence: Any) -> dict[str, Any]:
    """Assemble one finding dict, ready for :func:`emit_finding`."""
    tid = str(task.get("id"))
    return {
        "task_id": tid,
        "kind": kind,
        "finding_key": finding_key(tid, kind),
        "title": title,
        "detail": detail,
        "category": category,
        "severity": severity,
        "evidence": evidence,
    }


def finding_key(task_id: str, kind: str) -> str:
    return f"integrity:{task_id}:{kind}"


# --- Reconciliation (pure) --------------------------------------------------


def reconcile_task(
    task: dict[str, Any],
    delivery: dict[str, Any],
    *,
    trace: Optional[dict[str, Any]] = None,
    dod: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """All drift findings for one done card given its three axes of evidence."""
    out: list[dict[str, Any]] = []
    for detector in (
        lambda: detect_scratch_trap(task, delivery),
        lambda: detect_lost_delivery(task, delivery, trace),
        lambda: detect_coverage_gap(task, delivery, dod),
    ):
        found = detector()
        if found:
            out.append(found)
    return out


def reconcile(
    tasks: Iterable[dict[str, Any]],
    git_probe: GitProbe,
    *,
    langfuse_probe: Optional[LangfuseProbe] = None,
    dod_probe: Optional[DodProbe] = None,
) -> list[dict[str, Any]]:
    """Three-way reconcile every done card; return the flat list of findings.

    ``git_probe(task_id)`` yields the git delivery; the optional
    ``langfuse_probe`` / ``dod_probe`` supply the historical trail and the DoD
    facet (both absent today — see module docstring). Probes are called once per
    task and their failures degrade to "no evidence" rather than aborting the
    scan.
    """
    findings: list[dict[str, Any]] = []
    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid:
            continue
        delivery = _safe_probe(git_probe, tid, {"delivered": False, "files": []})
        trace = _safe_probe(langfuse_probe, tid, None) if langfuse_probe else None
        dod = _safe_probe(dod_probe, tid, None) if dod_probe else None
        findings.extend(reconcile_task(task, delivery, trace=trace, dod=dod))
    return findings


def _safe_probe(probe: Optional[Callable[[str], Any]], tid: str, default: Any) -> Any:
    """Call a probe, swallowing its errors into ``default`` (evidence absent)."""
    if probe is None:
        return default
    try:
        result = probe(tid)
    except Exception:
        return default
    return default if result is None else result


# --- Findings store ----------------------------------------------------------
#
# Mirrors the zeus ``findings`` schema shared with hermes_cli.regular_crons
# (``IF NOT EXISTS`` so whichever regular process runs first creates it; the DDL
# is identical, so there is no divergence). The two sources coexist by their
# ``source`` column (``integrity`` vs ``regular-crons``). NOTE: this DDL is
# duplicated across the two modules by necessity — see the follow-up to extract
# a shared ``findings_store`` once both land.
_FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    board         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    finding_key   TEXT NOT NULL,
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    category      TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL DEFAULT 'info',
    action_json   TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending',
    status        TEXT NOT NULL DEFAULT 'open',
    snooze_until  REAL,
    converted_task_id TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE(board, source, finding_key)
);
"""


def emit_finding(
    conn: sqlite3.Connection,
    finding: dict[str, Any],
    *,
    board: str,
    now: Optional[float] = None,
) -> None:
    """Upsert one open finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes title/detail/severity and ``updated_at`` but preserves
    ``created_at`` and never un-dismisses a finding a human already put to rest
    (dismissed/snoozed stay as-is).
    """
    now = time.time() if now is None else now
    conn.execute(_FINDINGS_SCHEMA)
    conn.execute(
        "INSERT INTO findings "
        "(board, source, finding_key, title, detail, evidence_json, category, "
        " severity, created_at, updated_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open') "
        "ON CONFLICT(board, source, finding_key) DO UPDATE SET "
        "  title=excluded.title, detail=excluded.detail, "
        "  evidence_json=excluded.evidence_json, category=excluded.category, "
        "  severity=excluded.severity, updated_at=excluded.updated_at, "
        "  status=CASE WHEN findings.status IN ('dismissed','snoozed','accepted') "
        "              THEN findings.status ELSE 'open' END",
        (board, FINDINGS_SOURCE, finding["finding_key"], finding["title"],
         finding["detail"], json.dumps(finding.get("evidence")),
         finding.get("category", ""), finding.get("severity", "info"), now, now),
    )
    conn.commit()


def clear_finding(conn: sqlite3.Connection, *, board: str, finding_key: str,
                  now: Optional[float] = None) -> None:
    """Mark a previously-open integrity finding obsolete (drift resolved)."""
    now = time.time() if now is None else now
    try:
        conn.execute(
            "UPDATE findings SET status='obsolete', updated_at=? "
            "WHERE source=? AND board=? AND finding_key=? AND status='open'",
            (now, FINDINGS_SOURCE, board, finding_key),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return  # no findings table yet -> nothing to clear


def scan_and_emit(
    tasks: Iterable[dict[str, Any]],
    findings: Iterable[dict[str, Any]],
    conn: Optional[sqlite3.Connection],
    *,
    board: str = "",
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Push each finding and clear any drift kind that recovered on a scanned card.

    Idempotent: run it every tick. For every task scanned this pass, each drift
    kind not present in ``findings`` is cleared (was open, now resolved).
    Returns the findings pushed this pass. ``conn is None`` (no zeus ledger) is a
    no-op — the reconcile is still computable, just without the push path.
    """
    findings = list(findings)
    if conn is None:
        return []
    by_task: dict[str, set[str]] = {}
    for f in findings:
        emit_finding(conn, f, board=board, now=now)
        by_task.setdefault(f["task_id"], set()).add(f["kind"])
    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid:
            continue
        active = by_task.get(tid, set())
        for kind in ALL_KINDS:
            if kind not in active:
                clear_finding(conn, board=board,
                              finding_key=finding_key(tid, kind), now=now)
    return findings


# --- Real wiring: git probe -------------------------------------------------


def _run_git(repo: Path, args: list[str]) -> str:
    """Run ``git -C <repo> <args>`` and return stdout ("" on any failure)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def delivery_for_task(repo: Path, task_id: str) -> dict[str, Any]:
    """Git delivery for ``task_id``: commits referencing it and files they touched.

    Searches every ref (``--all``) with a fixed-string grep so a card that
    landed on an unmerged branch still counts as delivered. ``files`` is the
    union of paths those commits touched.
    """
    out = _run_git(repo, ["log", "--all", "--format=%H",
                          "--fixed-strings", f"--grep={task_id}"])
    commits = [c for c in out.splitlines() if c.strip()]
    files: set[str] = set()
    for sha in commits:
        names = _run_git(repo, ["show", "--name-only", "--format=", sha])
        files.update(n for n in names.splitlines() if n.strip())
    return {"delivered": bool(commits), "commits": commits, "files": sorted(files)}


def default_git_probe(repo: Path) -> GitProbe:
    """A :data:`GitProbe` bound to ``repo`` (curries :func:`delivery_for_task`)."""
    return lambda task_id: delivery_for_task(repo, task_id)


# --- Real wiring: entry point ------------------------------------------------


def _task_to_dict(task: Any) -> dict[str, Any]:
    """Project a kanban ``Task`` (or dict) down to the fields the detectors use."""
    get = task.get if isinstance(task, dict) else lambda k: getattr(task, k, None)
    return {
        "id": get("id"),
        "title": get("title"),
        "branch_name": get("branch_name"),
        "project_id": get("project_id"),
        "workspace_kind": get("workspace_kind"),
    }


def load_done_tasks(board: Optional[str] = None) -> list[dict[str, Any]]:
    """Read done cards from the kanban DB, projected to detector-shaped dicts.

    Degrades to ``[]`` if the kanban module/DB is unavailable, so a host without
    a board simply has nothing to reconcile.
    """
    try:
        from hermes_cli import kanban_db
    except Exception:
        return []
    try:
        conn = kanban_db.connect(board=board)
    except Exception:
        return []
    try:
        rows = kanban_db.list_tasks(conn, status="done")
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [_task_to_dict(t) for t in rows]


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings, or ``None`` if the file is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching :func:`hermes_cli.regular_crons.open_findings_db`.
    """
    try:
        from hermes_cli import zeus_tokens
        path = zeus_tokens.default_zeus_db_path()
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def run_integrity_scan(
    *,
    repo: Path,
    board: Optional[str] = None,
    langfuse_probe: Optional[LangfuseProbe] = None,
    dod_probe: Optional[DodProbe] = None,
    conn: Optional[sqlite3.Connection] = None,
    emit: bool = True,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Reconcile the board's done cards against ``repo`` and push findings.

    Wires the real git probe over ``repo`` and the kanban DB for ``board``; the
    Langfuse and DoD probes are injectable (both absent today). When ``emit`` and
    a zeus.db exist, findings are upserted (and recovered ones cleared);
    otherwise the findings are just returned. Returns the findings for this pass.
    """
    tasks = load_done_tasks(board)
    findings = reconcile(
        tasks, default_git_probe(repo),
        langfuse_probe=langfuse_probe, dod_probe=dod_probe,
    )
    if not emit:
        return findings
    own_conn = conn is None
    if own_conn:
        conn = open_findings_db()
    try:
        return scan_and_emit(tasks, findings, conn, board=board or "", now=now)
    finally:
        if own_conn and conn is not None:
            conn.close()


# --- CLI ---------------------------------------------------------------------


def _render_human(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "Integrity: три оси согласны — дрейфа не найдено."
    lines = [f"Integrity: {len(findings)} находка(ок) дрейфа/покрытия:"]
    for f in findings:
        lines.append(f"  [{f['severity']}] {f['kind']}: {f['title']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.integrity_agent`` — the regular-process entry point."""
    parser = argparse.ArgumentParser(
        prog="integrity-agent",
        description="Three-way integrity reconciliation kanban↔git↔Langfuse.",
    )
    parser.add_argument("--repo", default=".", help="Repository to reconcile against.")
    parser.add_argument("--board", default=None, help="Kanban board slug (default: current).")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute findings only; do not push to the zeus store.")
    args = parser.parse_args(argv)

    repo = Path(os.path.expanduser(args.repo)).resolve()
    findings = run_integrity_scan(repo=repo, board=args.board, emit=not args.no_emit)

    if args.json:
        print(json.dumps({"finding_count": len(findings), "findings": findings},
                         ensure_ascii=False))
    else:
        print(_render_human(findings))
    # Non-zero exit iff any warning-or-worse drift is open, so a cron can alert.
    return 1 if any(f.get("severity") == "warning" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
