"""Liveness checks: IO probes (best-effort, never raise) and pure evaluators.

Each probe degrades to a benign value on any failure so a broken source can
never take the watchdog down — the whole point of the sealed core. The pure
:func:`evaluate` turns a plain :class:`Probe` snapshot plus a :class:`Config`
into a list of raw :class:`Condition` values; the runner decides which ones
are sustained long enough to alert on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config


@dataclass(frozen=True)
class Condition:
    """A raw fault signal. ``sustain_sec`` is how long it must hold before the
    runner alerts (0 = fire immediately)."""

    key: str
    summary: str
    sustain_sec: int = 0


@dataclass
class Probe:
    """A point-in-time snapshot of everything the evaluators need."""

    now: float
    gateway_pid: Optional[int]
    gateway_alive: bool
    log_age_sec: Optional[float]
    ready: int
    run: int
    stale_heartbeats: list[tuple[str, float]] = field(default_factory=list)
    corrupt_dbs: list[str] = field(default_factory=list)
    pool_recovered_after_limit: bool = False


# --- IO probes (best-effort) ------------------------------------------------

def read_gateway_pid(path: Path) -> Optional[int]:
    """Parse the gateway pid file (JSON ``{"pid": N}`` or a bare integer)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(json.loads(text)["pid"])
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return int(text)
    except ValueError:
        return None


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with ``pid`` exists (signal 0 probe)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def file_age_sec(path: Path, now: float) -> Optional[float]:
    """Seconds since ``path`` was last modified, or None if it is missing."""
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _connect_ro(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def query_queue(kanban_db: Path) -> tuple[int, int]:
    """Return ``(ready_assigned, running)`` task counts; ``(0, 0)`` on failure."""
    conn = _connect_ro(kanban_db)
    if conn is None:
        return (0, 0)
    try:
        ready = conn.execute(
            "SELECT count(*) FROM tasks WHERE status='ready' AND COALESCE(assignee,'')!=''"
        ).fetchone()[0]
        run = conn.execute(
            "SELECT count(*) FROM tasks WHERE status='running'"
        ).fetchone()[0]
        return (int(ready), int(run))
    except sqlite3.Error:
        return (0, 0)
    finally:
        conn.close()


def stale_heartbeats(kanban_db: Path, now: float, timeout_sec: int) -> list[tuple[str, float]]:
    """Running tasks whose last heartbeat (or start) is older than ``timeout_sec``.

    Returns ``[(task_id, age_sec), ...]``. Falls back to ``started_at`` when a
    task has no recorded heartbeat yet.
    """
    conn = _connect_ro(kanban_db)
    if conn is None:
        return []
    out: list[tuple[str, float]] = []
    try:
        rows = conn.execute(
            "SELECT id, last_heartbeat_at, started_at FROM tasks WHERE status='running'"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    for row in rows:
        beat = row["last_heartbeat_at"]
        if beat is None:
            beat = row["started_at"]
        if beat is None:
            continue
        age = now - float(beat)
        if age > timeout_sec:
            out.append((str(row["id"]), age))
    return out


def quick_check(db: Path) -> Optional[str]:
    """Run ``PRAGMA quick_check``; return 'ok', the first error, or None if the
    database cannot be opened at all."""
    conn = _connect_ro(db)
    if conn is None:
        return None
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        return str(exc)
    finally:
        conn.close()
    if not rows:
        return None
    first = str(rows[0][0])
    return "ok" if first.lower() == "ok" else first


def pool_recovered_after_limit(zeus_db: Path, now: float, window_sec: int) -> bool:
    """True when a subscription hit a usage limit recently and its cooldown has
    since elapsed — i.e. the pool *should* be usable again (auto-resume window).
    """
    conn = _connect_ro(zeus_db)
    if conn is None:
        return False
    try:
        recent = conn.execute(
            "SELECT count(*) FROM subscription_events"
            " WHERE kind='limit' AND ts > ?",
            (now - window_sec,),
        ).fetchone()[0]
        if not recent:
            return False
        recovered = conn.execute(
            "SELECT count(*) FROM claude_subscriptions"
            " WHERE enabled=1 AND last_limited_at IS NOT NULL"
            " AND (cooling_until IS NULL OR cooling_until <= ?)",
            (now,),
        ).fetchone()[0]
        return bool(recovered)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def gather(cfg: Config, now: Optional[float] = None) -> Probe:
    """Collect a full :class:`Probe` snapshot from the live system."""
    now = time.time() if now is None else now
    pid = read_gateway_pid(cfg.gateway_pid_file)
    ready, run = query_queue(cfg.kanban_db)
    corrupt = [
        name for name, db in (("kanban", cfg.kanban_db), ("zeus", cfg.zeus_db))
        if quick_check(db) not in (None, "ok")
    ]
    return Probe(
        now=now,
        gateway_pid=pid,
        gateway_alive=pid_alive(pid),
        log_age_sec=file_age_sec(cfg.gateway_log, now),
        ready=ready,
        run=run,
        stale_heartbeats=stale_heartbeats(cfg.kanban_db, now, cfg.heartbeat_timeout_sec),
        corrupt_dbs=corrupt,
        pool_recovered_after_limit=pool_recovered_after_limit(
            cfg.zeus_db, now, cfg.recent_limit_window_sec
        ),
    )


# --- Pure evaluation --------------------------------------------------------

def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def evaluate(probe: Probe, cfg: Config) -> list[Condition]:
    """Turn a snapshot into raw conditions. Pure — no IO, no clock."""
    conditions: list[Condition] = []

    if not probe.gateway_alive:
        pid = probe.gateway_pid
        detail = f"pid {pid} мёртв" if pid else "pid-файл отсутствует/пуст"
        conditions.append(Condition("gateway_dead", f"gateway-процесс не запущен ({detail})"))
    else:
        # Only meaningful when the gateway is alive; a dead gateway can't tick.
        age = probe.log_age_sec
        if age is not None and age > cfg.dispatcher_log_stale_sec:
            conditions.append(Condition(
                "dispatcher_stale",
                f"диспетчер не тикает: gateway.log молчит {_minutes(age)}м",
            ))

    for name in probe.corrupt_dbs:
        conditions.append(Condition(
            f"db_corrupt:{name}", f"{name}.db повреждена (quick_check не ok)",
        ))

    for task_id, age in probe.stale_heartbeats:
        conditions.append(Condition(
            f"heartbeat_stale:{task_id}",
            f"задача {task_id} running без heartbeat {_minutes(age)}м",
        ))

    if probe.ready > 0 and probe.run == 0:
        if probe.pool_recovered_after_limit:
            conditions.append(Condition(
                "resume_stuck",
                f"лимит был снят, но auto-resume не поднял очередь "
                f"(READY {probe.ready}, RUN 0)",
                sustain_sec=cfg.resume_grace_sec,
            ))
        else:
            conditions.append(Condition(
                "ready_no_run",
                f"есть готовые задачи, но ничего не выполняется "
                f"(READY {probe.ready}, RUN 0)",
                sustain_sec=cfg.ready_no_run_sec,
            ))

    return conditions
