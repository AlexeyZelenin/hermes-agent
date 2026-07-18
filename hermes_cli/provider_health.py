"""Provider health / blacklist store for the kanban dispatcher (task t_08676525).

Incident 2026-07-18: a worker was handed a Copilot ACP session whose auth
returned 403 (``set_model`` also failed with 'Method not found'); the session
never progressed, the task sat ``running`` with zero activity for 11 minutes,
and a human had to kill it. Root cause on the *provider* side: a provider with
failing auth was still handed to the next worker, silently, instead of being
health-gated first.

This module is the small persisted registry that closes that gap. It records,
per provider, whether the provider is currently usable:

* ``ok``        - no known problem; workers may be dispatched onto it.
* ``unhealthy`` - auto-marked after a failure (auth 403, capability block, a
  zero-activity hang). Carries a TTL (``unhealthy_until``) so it self-heals
  once the cooldown elapses and a fresh probe (the next dispatched worker) can
  re-confirm it. This is the machine verb.
* ``paused``    - an operator sticky pause from the UI/CLI ("провайдер сломан,
  пока не используем"). Never auto-expires; only ``resume`` clears it. This is
  the human verb the operator escalation asked for.

A "provider" here is the dispatcher-visible ACP channel identity - the same
``acp-<executor>`` label the ACP executor stamps on its run metadata (see
:func:`hermes_cli.kanban_db.provider_for_task`). The dispatcher only knows a
task's *executor*, not the ACP sub-backend it resolves to, so the health key is
executor-grained: pausing ``acp-claude-code`` gates every claude-code ACP
worker, and "respawn on a different provider" means the task is held off the
broken channel (reassign to ``codex``/``hermes-worker`` is the operator/UI verb).

Health is stored per board in that board's ``kanban.db`` - the same
single-writer DB the dispatcher already holds a lock on during a tick - so no
new global store or cross-process lock is introduced. Each board therefore
discovers a broken provider independently; a genuinely global outage surfaces on
every board's next tick.

Pure and dependency-injectable: every function takes an already-open sqlite
connection so it unit-tests against a temp DB. The *read* path
(:func:`availability` / :func:`is_available`) never creates the table and
degrades to "available" when it is missing, so a board that never saw a failure
carries zero overhead and the dispatcher gate is fail-open.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

STATUS_OK = "ok"
STATUS_UNHEALTHY = "unhealthy"
STATUS_PAUSED = "paused"

# How long an auto ``unhealthy`` mark holds before the provider is eligible for
# a fresh probe. Long enough that a broken-auth provider is not re-tried every
# 60s tick, short enough that a transient outage self-heals without an operator.
DEFAULT_UNHEALTHY_TTL_SECONDS = 1800  # 30 min

# The findings-store source namespace for provider-health cards, kept distinct
# from ``regular-crons`` so the two never collide on ``(board, source, key)``.
FINDINGS_SOURCE = "provider-health"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_health (
    provider        TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'ok',
    reason          TEXT NOT NULL DEFAULT '',
    unhealthy_until INTEGER,
    paused_by       TEXT NOT NULL DEFAULT '',
    consecutive     INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def record_unavailable(
    conn: sqlite3.Connection,
    provider: str,
    *,
    reason: str,
    ttl_seconds: int = DEFAULT_UNHEALTHY_TTL_SECONDS,
    now: Optional[int] = None,
) -> None:
    """Auto-mark ``provider`` unhealthy for ``ttl_seconds`` (a machine verb).

    Bumps a consecutive-failure counter for telemetry. Never overrides an
    operator ``paused`` state: a paused row keeps its status (the human decision
    wins) while still recording the fresh reason/counter underneath.
    """
    if not provider:
        return
    now = int(time.time()) if now is None else int(now)
    until = now + max(int(ttl_seconds), 0)
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO provider_health "
        "(provider, status, reason, unhealthy_until, consecutive, updated_at) "
        "VALUES (?, 'unhealthy', ?, ?, 1, ?) "
        "ON CONFLICT(provider) DO UPDATE SET "
        "  reason=excluded.reason, "
        "  unhealthy_until=excluded.unhealthy_until, "
        "  consecutive=provider_health.consecutive + 1, "
        "  updated_at=excluded.updated_at, "
        "  status=CASE WHEN provider_health.status='paused' "
        "              THEN 'paused' ELSE 'unhealthy' END",
        (provider, reason, until, now),
    )
    conn.commit()


def record_available(
    conn: sqlite3.Connection, provider: str, *, now: Optional[int] = None
) -> None:
    """Clear an auto ``unhealthy`` mark back to ``ok`` (resets the counter).

    A no-op on a missing row or an operator ``paused`` row - only ``resume``
    lifts a pause.
    """
    if not provider:
        return
    now = int(time.time()) if now is None else int(now)
    try:
        conn.execute(
            "UPDATE provider_health SET status='ok', reason='', "
            "unhealthy_until=NULL, consecutive=0, updated_at=? "
            "WHERE provider=? AND status='unhealthy'",
            (now, provider),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return


def pause(
    conn: sqlite3.Connection,
    provider: str,
    *,
    by: str = "",
    reason: str = "",
    now: Optional[int] = None,
) -> None:
    """Operator sticky pause: never auto-expires, only :func:`resume` clears it."""
    if not provider:
        return
    now = int(time.time()) if now is None else int(now)
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO provider_health "
        "(provider, status, reason, unhealthy_until, paused_by, updated_at) "
        "VALUES (?, 'paused', ?, NULL, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET "
        "  status='paused', reason=excluded.reason, unhealthy_until=NULL, "
        "  paused_by=excluded.paused_by, updated_at=excluded.updated_at",
        (provider, reason, by, now),
    )
    conn.commit()


def resume(
    conn: sqlite3.Connection, provider: str, *, now: Optional[int] = None
) -> None:
    """Lift an operator pause (and any residual unhealthy mark) back to ``ok``."""
    if not provider:
        return
    now = int(time.time()) if now is None else int(now)
    try:
        conn.execute(
            "UPDATE provider_health SET status='ok', reason='', "
            "unhealthy_until=NULL, paused_by='', consecutive=0, updated_at=? "
            "WHERE provider=?",
            (now, provider),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return


def availability(
    conn: sqlite3.Connection, provider: str, *, now: Optional[int] = None
) -> dict[str, Any]:
    """Current usability of ``provider`` as a dict.

    Shape: ``{"status": ok|unhealthy|paused, "reason": str, "until": int|None,
    "available": bool}``. An expired ``unhealthy`` TTL reports ``ok`` (lazy
    self-heal, read-only - it does not write). A missing table/row reports
    ``ok`` so the dispatcher gate is fail-open.
    """
    ok = {"status": STATUS_OK, "reason": "", "until": None, "available": True}
    if not provider:
        return ok
    now = int(time.time()) if now is None else int(now)
    try:
        row = conn.execute(
            "SELECT status, reason, unhealthy_until FROM provider_health "
            "WHERE provider=?",
            (provider,),
        ).fetchone()
    except sqlite3.OperationalError:
        return ok
    if row is None:
        return ok
    status = row["status"]
    if status == STATUS_PAUSED:
        return {
            "status": STATUS_PAUSED,
            "reason": row["reason"] or "",
            "until": None,
            "available": False,
        }
    if status == STATUS_UNHEALTHY:
        until = row["unhealthy_until"]
        if until is not None and now >= int(until):
            return ok  # cooldown elapsed -> eligible for a fresh probe
        return {
            "status": STATUS_UNHEALTHY,
            "reason": row["reason"] or "",
            "until": int(until) if until is not None else None,
            "available": False,
        }
    return ok


def is_available(
    conn: sqlite3.Connection, provider: str, *, now: Optional[int] = None
) -> bool:
    """True when a worker may be dispatched onto ``provider`` right now."""
    return bool(availability(conn, provider, now=now)["available"])


def list_health(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[dict[str, Any]]:
    """Every provider with a non-``ok`` (or historically-marked) row, for the
    UI / CLI. Expired unhealthy rows are folded to ``ok`` like
    :func:`availability`. A missing table degrades to an empty list."""
    now = int(time.time()) if now is None else int(now)
    try:
        rows = conn.execute(
            "SELECT provider, status, reason, unhealthy_until, paused_by, "
            "consecutive, updated_at FROM provider_health ORDER BY provider"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        avail = availability(conn, row["provider"], now=now)
        out.append(
            {
                "provider": row["provider"],
                "status": avail["status"],
                "reason": avail["reason"],
                "until": avail["until"],
                "paused_by": row["paused_by"] or "",
                "consecutive": int(row["consecutive"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "available": avail["available"],
            }
        )
    return out


# --- Findings surfacing (the operator's "Проблемы" tab) ----------------------


def _finding_key(provider: str) -> str:
    return f"provider:{provider}"


def emit_health_finding(
    provider: str,
    *,
    status: str,
    reason: str,
    board: str = "",
    now: Optional[float] = None,
) -> None:
    """Best-effort: push a provider-health card into the shared findings store.

    Global (``board=''``) system-level card so it surfaces at the top-level
    "Проблемы" view rather than buried in a project. Carries a structured
    ``action`` proposing the operator pause verb the escalation asked for. A
    host with no zeus ledger (no findings DB) is a silent no-op.
    """
    try:
        from hermes_cli import regular_crons
    except Exception:
        return
    conn = None
    try:
        conn = regular_crons.open_findings_db()
        if conn is None:
            return
        title = f"Провайдер {provider} недоступен"
        detail = (
            f"Провайдер {provider} помечен как «{status}»: {reason}. "
            "Пока он в этом состоянии, задачи на него не диспатчатся "
            "(и не респавнятся на сломанный канал)."
        )
        action = {
            "proposed": (
                f"Провайдер {provider} сломан ({reason}). Пока не используем. "
                "Поставить на паузу?"
            ),
            "verb": "pause_provider",
            "provider": provider,
        }
        regular_crons.emit_finding(
            conn,
            board=board,
            source=FINDINGS_SOURCE,
            finding_key=_finding_key(provider),
            title=title,
            detail=detail,
            category="reliability",
            severity="high",
            evidence={"provider": provider, "status": status, "reason": reason},
            action=action,
            now=now,
        )
    except Exception:
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def clear_health_finding(
    provider: str, *, board: str = "", now: Optional[float] = None
) -> None:
    """Best-effort: mark a provider-health card obsolete once the provider recovers."""
    try:
        from hermes_cli import regular_crons
    except Exception:
        return
    conn = None
    try:
        conn = regular_crons.open_findings_db()
        if conn is None:
            return
        regular_crons.clear_finding(
            conn, board=board, finding_key=_finding_key(provider),
            source=FINDINGS_SOURCE, now=now,
        )
    except Exception:
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
