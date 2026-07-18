"""Vision reconciliation — periodic sverka бэклога с ``knowledge/vision.md``.

Operator model (task t_f12b86f3): an extension of reflection/advisor (see
:mod:`hermes_cli.regular_crons`, :mod:`hermes_cli.integrity_agent`). Where the
triage->todo gate (:mod:`hermes_cli.kanban_specify`) checks one card AT INTAKE,
this regular process re-measures the WHOLE backlog against the project vision on
a rhythm and catches drift that accumulates after intake:

* **orphan** — a card that no longer advances anything in the vision (out of
  scope / redundant / a half-solution the vision warns against);
* **contradiction** — a card that contradicts the "What we do NOT do" section.

The judgement is semantic, so it is delegated to an injectable ``judge`` — the
default wiring makes ONE batch LLM call over the whole backlog (cheap, keeps the
per-card cost bounded); tests inject a deterministic fake. Everything downstream
of the judge is pure and dependency-injectable.

Findings land in the shared zeus ``findings`` store tagged ``source=vision``
(the same store the integrity / security / regular-cron scanners push to), so
they surface in Проблемы exactly like every other regular-process finding — a
human accepts one (→ a Triage card) or dismisses it. Only drift pushes; a clean
backlog stays quiet, and a card that recovered has its finding cleared on the
next pass. Degrades to a bare, side-effect-free scan when the vision doc, the
board, or the zeus ledger is absent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from hermes_cli import vision as vision_mod

logger = logging.getLogger(__name__)

FINDINGS_SOURCE = "vision"

# Drift kinds. Stable strings — they form part of the finding_key, so renaming
# one orphans its prior findings.
KIND_ORPHAN = "orphan"
KIND_CONTRADICTION = "contradiction"
ALL_KINDS = (KIND_ORPHAN, KIND_CONTRADICTION)

# A ``judge`` takes (backlog_tasks, vision_text) and returns raw drift dicts
# ``{"task_id", "kind", "reason", "severity"?}``. Unknown kinds / task ids are
# dropped by :func:`reconcile`, so a hallucinating judge cannot invent findings.
Judge = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]

# Statuses that make up the "backlog" worth reconciling: planned-but-not-done
# work. Done/archived cards are the integrity agent's job, not the vision's.
BACKLOG_STATUSES = ("triage", "todo", "ready")

_DEFAULT_SEVERITY = "warning"


# --- Reconciliation (pure) --------------------------------------------------


def finding_key(task_id: str, kind: str) -> str:
    return f"vision:{task_id}:{kind}"


def _finding(task_id: str, title: str, kind: str, reason: str,
             severity: str) -> dict[str, Any]:
    """Shape one drift dict into a finding ready for :func:`emit_finding`."""
    label = ("сирота (вне видения)" if kind == KIND_ORPHAN
             else "противоречит видению")
    detail = reason.strip() or f"Задача {task_id}: {label}."
    return {
        "task_id": task_id,
        "kind": kind,
        "finding_key": finding_key(task_id, kind),
        "title": f"«{title or task_id}»: {label}",
        "detail": detail,
        "category": "vision-drift",
        "severity": severity if severity else _DEFAULT_SEVERITY,
        "evidence": {"kind": kind, "reason": reason},
    }


def reconcile(
    tasks: Iterable[dict[str, Any]],
    vision_text: str,
    judge: Judge,
) -> list[dict[str, Any]]:
    """Judge the backlog against the vision; return validated drift findings.

    Only drift dicts whose ``task_id`` is a real scanned card and whose ``kind``
    is known survive — a judge that hallucinates ids/kinds cannot forge a
    finding. At most one finding per (task, kind) pair. A judge that raises
    degrades to "no drift" rather than aborting the whole scan.
    """
    tasks = list(tasks)
    by_id = {str(t.get("id")): t for t in tasks if t.get("id")}
    if not by_id or not vision_text:
        return []
    try:
        raw = judge(tasks, vision_text)
    except Exception as exc:
        logger.info("vision-reconcile: judge failed: %s", exc)
        return []
    seen: set[tuple[str, str]] = set()
    findings: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if tid not in by_id or kind not in ALL_KINDS or (tid, kind) in seen:
            continue
        seen.add((tid, kind))
        title = str(by_id[tid].get("title") or tid)
        reason = str(item.get("reason") or "").strip()
        severity = str(item.get("severity") or "").strip().lower()
        findings.append(_finding(tid, title, kind, reason, severity))
    return findings


# --- Findings store ----------------------------------------------------------
#
# Mirrors the shared zeus ``findings`` schema (see hermes_cli.integrity_agent /
# hermes_cli.regular_crons / hermes_cli.security_review). Same DDL, IF NOT
# EXISTS so whichever regular process runs first creates it; distinguished by
# source=vision. The DDL is duplicated across the sources by necessity — see the
# noted follow-up to extract a shared ``findings_store``.
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


def emit_finding(conn: sqlite3.Connection, finding: dict[str, Any], *,
                 board: str, now: Optional[float] = None) -> None:
    """Upsert one open vision finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes title/detail/severity + ``updated_at`` but preserves
    ``created_at`` and never un-dismisses a finding a human already put to rest.
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
    """Mark a previously-open vision finding obsolete (drift resolved)."""
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
    kind not present in ``findings`` is cleared (was open, now resolved), so a
    card the operator reworded back into scope stops nagging. ``conn is None``
    (no zeus ledger) is a no-op — the reconcile is still computable.
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


# --- Real wiring: backlog + judge -------------------------------------------


def _task_to_dict(task: Any) -> dict[str, Any]:
    """Project a kanban ``Task`` (or dict) to the fields the judge reads."""
    get = task.get if isinstance(task, dict) else lambda k: getattr(task, k, None)
    return {"id": get("id"), "title": get("title"), "body": get("body"),
            "status": get("status")}


def load_backlog_tasks(board: Optional[str] = None) -> list[dict[str, Any]]:
    """Read the planned-but-not-done backlog, projected to judge-shaped dicts.

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
        rows: list[Any] = []
        for status in BACKLOG_STATUSES:
            rows.extend(kanban_db.list_tasks(conn, status=status))
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [_task_to_dict(t) for t in rows]


_JUDGE_SYSTEM = """You audit a Kanban backlog against the project vision.
For each card, decide whether it still fits the picture. Report ONLY drift —
cards that do NOT fit. Two kinds:
  - "orphan": out of scope / redundant / a half-solution the vision warns
    against (it advances nothing in the vision).
  - "contradiction": it contradicts the "What we do NOT do" section.
A card that fits is NOT reported. When in doubt, do NOT report it — this guards
against obvious drift, not legitimate work.

Output a single JSON object, no prose, no code fences:
  {"findings": [{"task_id": "<id>", "kind": "orphan|contradiction",
                 "reason": "<one short sentence>"}]}
Return {"findings": []} when nothing drifts."""

_JUDGE_MAX_TOKENS = 4000
_JUDGE_BODY_CHARS = 600


def _judge_user_msg(tasks: list[dict[str, Any]], vision_text: str) -> str:
    lines = ["--- PROJECT VISION (knowledge/vision.md) ---", vision_text,
             "--- END PROJECT VISION ---", "", "Backlog cards:"]
    for t in tasks:
        body = str(t.get("body") or "").strip().replace("\n", " ")
        if len(body) > _JUDGE_BODY_CHARS:
            body = body[:_JUDGE_BODY_CHARS] + "…"
        lines.append(f"- id={t.get('id')} | {t.get('title') or ''} | {body}")
    return "\n".join(lines)


def _parse_judge_reply(raw: str) -> list[dict[str, Any]]:
    """Lenient extraction of the ``findings`` array from the model reply."""
    if not raw:
        return []
    first, last = raw.find("{"), raw.rfind("}")
    if first == -1 or last <= first:
        return []
    try:
        obj = json.loads(raw[first:last + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    items = obj.get("findings") if isinstance(obj, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def default_judge(*, timeout: Optional[int] = None) -> Judge:
    """A :data:`Judge` backed by one batch ``call_llm`` over the whole backlog.

    Routes through the ``triage_specifier`` auxiliary task (point it at a
    planner-class model — Fable — via ``hermes model``), matching the gate so
    both the intake check and this sweep reason with the same planner.
    """
    def _judge(tasks: list[dict[str, Any]], vision_text: str) -> list[dict[str, Any]]:
        from agent.auxiliary_client import call_llm
        resp = call_llm(
            task="triage_specifier",
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": _judge_user_msg(tasks, vision_text)},
            ],
            temperature=0.2,
            max_tokens=_JUDGE_MAX_TOKENS,
            timeout=timeout or 180,
        )
        try:
            raw = (resp.choices[0].message.content or "").strip()
        except Exception:
            raw = ""
        return _parse_judge_reply(raw)

    return _judge


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings, or ``None`` if the file is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching :func:`hermes_cli.integrity_agent.open_findings_db`.
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


def run_vision_reconcile(
    *,
    board: Optional[str] = None,
    vision_text: Optional[str] = None,
    judge: Optional[Judge] = None,
    conn: Optional[sqlite3.Connection] = None,
    emit: bool = True,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Reconcile the board's backlog against the vision and push findings.

    Loads the vision doc and the backlog, judges drift (batch LLM by default),
    and — when ``emit`` and a zeus.db exist — upserts findings and clears
    recovered ones. A missing vision doc or empty backlog is a clean no-op.
    Returns the findings for this pass.
    """
    text = vision_text if vision_text is not None else vision_mod.load_vision_text()
    tasks = load_backlog_tasks(board)
    if not text or not tasks:
        return []
    findings = reconcile(tasks, text, judge or default_judge())
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


# --- Regular cron seeding ---------------------------------------------------

JOB_ORIGIN = {"kind": "vision-reconcile"}
_RUNNER_SCRIPT_NAME = "vision_reconcile_cron.py"
_RUNNER_SCRIPT_BODY = (
    "# Auto-generated by hermes_cli.vision_reconcile — backlog↔vision sverka\n"
    "# cron runner (task t_f12b86f3). Managed by the regular-process seeder.\n"
    "from hermes_cli.vision_reconcile import main\n"
    "raise SystemExit(main())\n"
)
_DEFAULT_SCHEDULE = "30 4 * * *"  # daily 04:30 (calendar cadence, off-peak)


def _write_runner_script() -> Optional[str]:
    """Write the thin runner into ``HERMES_HOME/scripts`` and return its name."""
    try:
        from hermes_constants import get_hermes_home
        scripts_dir = get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / _RUNNER_SCRIPT_NAME).write_text(
            _RUNNER_SCRIPT_BODY, encoding="utf-8")
        return _RUNNER_SCRIPT_NAME
    except Exception as exc:
        logger.debug("could not write vision-reconcile runner: %s", exc)
        return None


def ensure_vision_reconcile_job(*, schedule: str = _DEFAULT_SCHEDULE
                                ) -> Optional[dict[str, Any]]:
    """Idempotently register the vision-reconcile cron (reflection cadence).

    Runs the sverka runner as a ``no_agent`` script job on a daily calendar
    schedule. The name carries "self-review" so :mod:`hermes_cli.regular_crons`
    buckets it under Рефлексия. Safe to call on every boot/tick — an
    already-registered job short-circuits. Returns the existing or newly created
    job, or ``None`` when the cron store is unavailable.
    """
    try:
        from cron import jobs as cron_jobs
    except Exception:
        return None
    try:
        for job in cron_jobs.load_jobs():
            if (job.get("origin") or {}).get("kind") == JOB_ORIGIN["kind"]:
                return job
        script = _write_runner_script()
        if script is None:
            return None
        return cron_jobs.create_job(
            prompt=None, schedule=schedule,
            name="Vision self-review: сверка бэклога с видением",
            script=script, no_agent=True, deliver="local",
            origin=dict(JOB_ORIGIN))
    except Exception as exc:
        logger.debug("could not ensure vision-reconcile cron: %s", exc)
        return None


# --- CLI --------------------------------------------------------------------


def _render_human(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "Vision: бэклог согласуется с видением, дрейфа нет."
    lines = [f"Vision: {len(findings)} находка(ок) дрейфа:"]
    for f in findings:
        lines.append(f"  [{f['severity']}] {f['kind']}: {f['title']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.vision_reconcile`` — the regular-process entry."""
    parser = argparse.ArgumentParser(
        prog="vision-reconcile",
        description="Periodic backlog↔vision reconciliation (drift → Проблемы).")
    parser.add_argument("--board", default=None, help="Kanban board slug.")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute findings only; do not push to the zeus store.")
    args = parser.parse_args(argv)

    findings = run_vision_reconcile(board=args.board, emit=not args.no_emit)
    if args.json:
        print(json.dumps({"finding_count": len(findings), "findings": findings},
                         ensure_ascii=False))
    else:
        print(_render_human(findings))
    # Non-zero exit iff any warning-or-worse drift is open, so a cron can alert.
    return 1 if any(f.get("severity") == "warning" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
