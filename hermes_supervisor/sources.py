"""Build supervisor snapshots from live hermes-agent data.

Sources:
  * running runs   -> ``kanban_db`` (tasks joined to their current ``task_runs`` row)
  * token totals   -> ``zeus.db`` ``token_usage`` (summed per ``task_id``; the ledger the
                      operator pointed at, so no separate rate log is needed)
  * workspace mtime -> newest file mtime under ``tasks.workspace_path`` (feeds the R4
                      stale-workspace loop detector)

Everything here is read-only; a missing ``token_usage`` table (zeus plugin absent) or an
unreadable workspace degrades to ``None``/0 rather than raising.
"""

import os
import sqlite3

from session_supervisor.snapshots import DispatcherSnapshot, RunSnapshot

# Directories that never count as "worker progress" when timing the workspace.
_MTIME_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
_MTIME_FILE_CAP = 4000


def _latest_mtime(path: str | None, cap: int = _MTIME_FILE_CAP) -> float | None:
    """Newest mtime in the workspace subtree, or ``None`` if it can't be read.

    A bounded walk (``cap`` files) keeps the per-tick cost flat on large trees; the
    supervisor only needs "did anything change recently", not an exact figure.
    """
    if not path or not os.path.isdir(path):
        return None
    latest = 0.0
    seen = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _MTIME_SKIP_DIRS]
        for name in files:
            try:
                mtime = os.stat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            if mtime > latest:
                latest = mtime
            seen += 1
            if seen >= cap:
                return latest or None
    return latest or None


def token_usage_totals(zeus_conn: sqlite3.Connection, task_id: str) -> tuple[int, float | None]:
    """``(tokens_total, last_usage_ts)`` for a task from the zeus token ledger."""
    try:
        row = zeus_conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS total, MAX(ts) AS last_ts "
            "FROM token_usage WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, None  # ledger table absent (zeus plugin not installed)
    if row is None:
        return 0, None
    total = int(row["total"] or 0)
    last_ts = float(row["last_ts"]) if row["last_ts"] is not None else None
    return total, last_ts


def _attempt_counts(kb_conn: sqlite3.Connection) -> dict[str, int]:
    rows = kb_conn.execute(
        "SELECT task_id, COUNT(*) AS n FROM task_runs GROUP BY task_id"
    ).fetchall()
    return {r["task_id"]: int(r["n"]) for r in rows}


_RUNNING_RUNS_SQL = (
    "SELECT t.id AS task_id, t.title AS title, t.session_id AS session_id, "
    "       t.workspace_path AS workspace_path, t.worker_pid AS task_pid, "
    "       t.last_heartbeat_at AS task_hb, t.started_at AS task_started, "
    "       r.id AS run_id, r.claim_lock AS claim_lock, r.worker_pid AS run_pid, "
    "       r.last_heartbeat_at AS run_hb, r.started_at AS run_started "
    "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
    "WHERE t.status = 'running'"
)


def _row_to_snapshot(row, board, now, tokens_total, last_token_ts, attempt, budget) -> RunSnapshot:
    task_id = row["task_id"]
    heartbeat = row["run_hb"] if row["run_hb"] is not None else row["task_hb"]
    started = row["run_started"] if row["run_started"] is not None else row["task_started"]
    worker_pid = row["run_pid"] if row["run_pid"] is not None else row["task_pid"]
    workspace = row["workspace_path"]
    return RunSnapshot(
        run_id=str(row["run_id"]) if row["run_id"] is not None else f"task:{task_id}",
        board=board,
        task_id=task_id,
        task_title=row["title"] or task_id,
        session_id=row["session_id"] or "",
        worker_id=row["claim_lock"] or (f"pid:{worker_pid}" if worker_pid else "unknown"),
        attempt=attempt,
        status="running",
        started_at=float(started) if started is not None else now,
        last_heartbeat_at=float(heartbeat) if heartbeat is not None else None,
        last_token_usage_at=last_token_ts,
        tokens_total=tokens_total,
        token_budget=budget,
        log_ref=workspace or "",
        recent_tool_calls=(),
        workspace_changed_at=_latest_mtime(workspace),
    )


def build_run_snapshots(
    kb_conn: sqlite3.Connection,
    board: str,
    zeus_conn: sqlite3.Connection | None,
    now: float,
    *,
    budget_lookup=None,
) -> list[RunSnapshot]:
    """Snapshot every ``running`` task on ``board`` for one supervision pass.

    ``budget_lookup`` is an optional ``task_id -> int | None`` callable (Take's budget
    decomposition); without it ``token_budget`` is ``None`` and only R5's hard cap applies.
    ``recent_tool_calls`` is left empty: R4's identical-call detector needs a session
    transcript reader that hermes-agent does not expose yet (see INTEGRATION.md); the R4
    stale-workspace detector still fires from ``workspace_changed_at`` + token growth.
    """
    rows = kb_conn.execute(_RUNNING_RUNS_SQL).fetchall()
    attempts = _attempt_counts(kb_conn)
    snapshots: list[RunSnapshot] = []
    for row in rows:
        task_id = row["task_id"]
        tokens_total, last_token_ts = (
            token_usage_totals(zeus_conn, task_id) if zeus_conn is not None else (0, None)
        )
        budget = budget_lookup(task_id) if budget_lookup is not None else None
        snapshots.append(
            _row_to_snapshot(
                row, board, now, tokens_total, last_token_ts,
                attempts.get(task_id, 1), budget,
            )
        )
    return snapshots


def count_running(kb_conn: sqlite3.Connection) -> int:
    row = kb_conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
    ).fetchone()
    return int(row["n"]) if row else 0


def build_dispatcher_snapshot(
    board: str,
    *,
    ready_queue_size: int,
    spawns_last_tick: int,
    free_slots: int,
    log_ref: str = "",
) -> DispatcherSnapshot:
    """Wrap the dispatcher-tick figures the gateway loop already computes.

    The embedded dispatcher (``gateway/kanban_watchers.py``) knows, per tick, whether the
    spawnable-ready queue is non-empty, whether it spawned anything, and the board
    ``agent_limit``; ``free_slots = agent_limit - running`` closes R6's "slots free but
    nothing spawned" signature.
    """
    return DispatcherSnapshot(
        board=board,
        ready_queue_size=max(0, ready_queue_size),
        spawns_last_tick=max(0, spawns_last_tick),
        free_slots=max(0, free_slots),
        log_ref=log_ref,
    )
