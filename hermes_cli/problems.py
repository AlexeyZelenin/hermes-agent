"""Проблемы — a browse-only UI model over the shared findings store.

Task t_e9b93153. The findings store (the zeus.db ``findings`` table, written by
the reflection / advisor / security-review / integrity / cross-card scanners) is
the single Ось-B registry of things that want a human's attention. This module
is the *read* side of that store plus the two human verbs the UI offers on each
finding:

* **accept**  → materialise the finding as a real backlog card (a ``triage``
  task) and stamp the finding ``accepted`` / ``converted_task_id``;
* **dismiss** → mark the finding resolved-by-human so re-scans never resurface
  it (the emitters already preserve ``dismissed`` / ``snoozed`` / ``accepted``).

Global problems (``board = ''``) are system-level — reflecting agents, engine
health — and surface as a top-level "Проблемы" menu item so they are never
buried inside the sealed engine project. Per-board problems (``board = <slug>``)
surface as a section inside that board, hidden when empty. The distinction is
purely the ``board`` column; this module never special-cases a "sealed" project.

Pure and dependency-injectable: every function takes an already-open sqlite
connection, so it unit-tests against a temp db with no live zeus/kanban store.
This is the browse view only — it never *writes* findings; producers own that
push path (see :mod:`hermes_cli.regular_crons`, :mod:`hermes_cli.integrity_agent`).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

# Human-decision statuses the producers' upsert preserves on re-emit. Keep these
# in sync with the ``ON CONFLICT`` CASE in ``regular_crons.emit_finding`` /
# ``integrity_agent.emit_finding``.
OPEN = "open"
DISMISSED = "dismissed"
SNOOZED = "snoozed"
ACCEPTED = "accepted"

# Severity taxonomy. The store is schema-free on severity (any TEXT, default
# ``info``); scanners in the wild use info/low/warning/medium/high/critical and
# synonyms. We fold them into a single rank so the UI can sort "worst first" and
# reuse the board's existing three-rung diagnostic palette (warning/error/
# critical) for colour.
_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "notice": 1,
    "minor": 1,
    "warning": 2,
    "warn": 2,
    "medium": 2,
    "moderate": 2,
    "high": 3,
    "error": 3,
    "major": 3,
    "critical": 4,
    "crit": 4,
    "fatal": 4,
    "blocker": 4,
}
_TONE_BY_RANK = ("info", "info", "warning", "error", "critical")

# Columns we read for the model. Named explicitly (not ``*``) so the shape is
# stable regardless of column order in a self-healed table.
_COLUMNS = (
    "id, board, source, finding_key, title, detail, evidence_json, category, "
    "severity, action_json, review_status, status, snooze_until, "
    "converted_task_id, created_at, updated_at"
)


def severity_rank(severity: Optional[str]) -> int:
    """Fold a free-form severity string into a 0..4 rank (unknown → 0/info)."""
    return _SEVERITY_RANK.get((severity or "").strip().lower(), 0)


def severity_tone(severity: Optional[str]) -> str:
    """Map a severity onto the board's diagnostic tone bucket for colouring."""
    return _TONE_BY_RANK[severity_rank(severity)]


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _proposed_from_action(action_json: Optional[str]) -> str:
    """Pull the proposed fix out of ``action_json`` if a producer wrote one.

    The store provisions ``action_json`` for exactly this; today's producers
    leave it ``{}``, so this degrades to an empty proposal (browse-only).
    """
    try:
        action = json.loads(action_json or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(action, dict):
        return ""
    for key in ("proposed", "solution", "fix", "remedy", "suggestion"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def problem_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape one findings row into the draft-card model the UI renders."""
    severity = _row_get(row, "severity") or "info"
    try:
        evidence = json.loads(_row_get(row, "evidence_json") or "[]")
    except (ValueError, TypeError):
        evidence = []
    if not isinstance(evidence, list):
        evidence = [evidence]
    return {
        "id": _row_get(row, "id"),
        "board": _row_get(row, "board") or "",
        "source": _row_get(row, "source") or "",
        "finding_key": _row_get(row, "finding_key") or "",
        "title": _row_get(row, "title") or "",
        "explanation": _row_get(row, "detail") or "",
        "proposed": _proposed_from_action(_row_get(row, "action_json")),
        "severity": severity,
        "severity_rank": severity_rank(severity),
        "tone": severity_tone(severity),
        "category": _row_get(row, "category") or "",
        "evidence": evidence,
        "status": _row_get(row, "status") or OPEN,
        "converted_task_id": _row_get(row, "converted_task_id") or "",
        "created_at": _row_get(row, "created_at"),
        "updated_at": _row_get(row, "updated_at"),
    }


def list_problems(
    conn: Optional[sqlite3.Connection],
    *,
    board: Optional[str] = None,
    statuses: tuple[str, ...] = (OPEN,),
) -> list[dict[str, Any]]:
    """Open findings as draft cards, worst-severity first.

    ``board=None`` → every board (global + per-project); ``board=''`` → global
    only; ``board=<slug>`` → that board only. A ``None`` connection (no zeus
    ledger) or a missing ``findings`` table both degrade to an empty list, so
    the UI simply shows nothing rather than erroring.
    """
    if conn is None or not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    where = [f"status IN ({placeholders})"]
    params: list[Any] = list(statuses)
    if board is not None:
        where.append("board = ?")
        params.append(board)
    sql = f"SELECT {_COLUMNS} FROM findings WHERE {' AND '.join(where)}"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # no findings table yet → nothing to browse
    problems = [problem_from_row(row) for row in rows]
    problems.sort(key=lambda p: (-p["severity_rank"], -(p["updated_at"] or 0)))
    return problems


def get_problem(
    conn: Optional[sqlite3.Connection], finding_id: int
) -> Optional[dict[str, Any]]:
    if conn is None:
        return None
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return problem_from_row(row) if row is not None else None


def dismiss_problem(
    conn: sqlite3.Connection, finding_id: int, *, now: Optional[float] = None
) -> bool:
    """Mark an open finding resolved-by-human. Returns True if one was updated."""
    now = time.time() if now is None else now
    try:
        cur = conn.execute(
            "UPDATE findings SET status = ?, review_status = 'dismissed', "
            "updated_at = ? WHERE id = ? AND status = ?",
            (DISMISSED, now, finding_id, OPEN),
        )
    except sqlite3.OperationalError:
        return False
    conn.commit()
    return cur.rowcount > 0


def _card_body(problem: dict[str, Any]) -> str:
    """Compose the backlog card body: explanation + proposed fix + provenance."""
    parts: list[str] = []
    explanation = problem["explanation"].strip()
    if explanation:
        parts.append(explanation)
    if problem["proposed"]:
        parts.append("## Предложенное решение\n\n" + problem["proposed"])
    parts.append(
        f"---\n_Из находки `{problem['finding_key']}` "
        f"(источник: {problem['source']}, важность: {problem['severity']})._"
    )
    return "\n\n".join(parts)


def accept_problem(
    findings_conn: sqlite3.Connection,
    finding_id: int,
    *,
    kanban_conn: Optional[sqlite3.Connection] = None,
    created_by: str = "problems",
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Turn an open finding into a real ``triage`` backlog card.

    Creates the card on the finding's own board (``board=''`` → the active/
    default board), then stamps the finding ``accepted`` with the new task id so
    re-scans keep it out of the browse view. Pass ``kanban_conn`` to reuse an
    already-open board connection (tests / same-board routes); otherwise a board
    connection is opened and closed here. Returns ``{task_id, board}`` or
    ``None`` when the finding isn't open.
    """
    now = time.time() if now is None else now
    try:
        row = findings_conn.execute(
            f"SELECT {_COLUMNS} FROM findings WHERE id = ? AND status = ?",
            (finding_id, OPEN),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None

    from hermes_cli import kanban_db

    problem = problem_from_row(row)
    board = problem["board"] or None
    owned_conn: Optional[sqlite3.Connection] = None
    conn = kanban_conn
    if conn is None:
        try:
            kanban_db.init_db(board=board)
        except Exception:
            pass  # connect() below still self-heals / raises meaningfully
        owned_conn = kanban_db.connect(board=board)
        conn = owned_conn
    try:
        task_id = kanban_db.create_task(
            conn,
            title=problem["title"] or "Проблема без названия",
            body=_card_body(problem),
            triage=True,
            created_by=created_by,
            category=(problem["category"] or None),
        )
    finally:
        if owned_conn is not None:
            owned_conn.close()

    findings_conn.execute(
        "UPDATE findings SET status = ?, review_status = 'accepted', "
        "converted_task_id = ?, updated_at = ? WHERE id = ?",
        (ACCEPTED, task_id, now, finding_id),
    )
    findings_conn.commit()
    return {"task_id": task_id, "board": problem["board"]}


def open_store() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for the findings store, or ``None`` if absent."""
    from hermes_cli import regular_crons

    return regular_crons.open_findings_db()
