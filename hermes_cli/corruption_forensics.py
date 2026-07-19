"""Corruption forensics — turn an anonymous kanban.db corruption episode into a
named, time-correlated report (task t_f135b0b6).

The overseer's ``heal_kanban.sh`` already snaps a first-layer evidence file at
detect time (``lsof`` on the db/wal/shm, ``ps`` of live hermes processes,
``gateway.log`` tail) so a human can see WHO held the file. This module is the
second layer, inside the product: when the DB self-heals (or a structural
episode is detected), it correlates the moment of detection against the
``task_events`` feed — every run that wrote a ``tool_call`` in a ±60s window is a
*suspect*, and its last tool call plus its worker pid and run outcome are the
forensic breadcrumbs. Pure time-correlation over data already in the board; no
LLM.

Two sinks, both best-effort (a forensics failure must never break the DB
repair):

* **engine_log** (``category='self_heal'``) — the enriched episode payload
  (corruption class + suspect list) lands on the same self-heal breadcrumb the
  auto-repair already writes, so the "под капотом" viewer shows who was writing.
  Written only on the healed (index-only) path, where the connection is proven
  healthy again post-REINDEX.
* **findings** (the shared zeus.db store read by :mod:`hermes_cli.problems`) —
  one draft card per episode surfaces in the Проблемы/решения UI with the time,
  the corruption class (index-only vs structural), and the suspected tasks.
  Emitted for every episode, healed or not, since the findings store is a
  separate healthy DB even when kanban.db is quarantined.

Crash-badge link (task t_e5997536): each suspect carries its run's
``worker_pid`` and ``outcome``. When the heal kills the writers, their runs end
up ``crashed``/``reclaimed``/``timed_out`` — the exact signal the crash-badge
diagnostic (``kanban_diagnostics._rule_repeated_crashes``) counts. Suspects in
that state are flagged so the operator can jump from the episode card to the
crash badge on the culprit card.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# Detection is a proxy for the corrupting write: the proactive sweep runs on a
# 60s cadence and the open-path guard can fire on the next fresh connect, so the
# real write sits somewhere shortly before ``at``. A symmetric ±60s window round
# the detection timestamp catches the writers that were active either side of it
# without dragging in unrelated history.
WINDOW_SECONDS = 60
# Cap the suspect list so the engine_log payload stays under the store's
# MAX_PAYLOAD_CHARS (4000) and the card stays readable. Closest-to-detection
# writers are kept first.
MAX_SUSPECTS = 20
# Bound the window scan so a dispatcher burst (one tool_call row per tool per
# worker) can't make forensics itself expensive on a rare detection event.
_MAX_WINDOW_ROWS = 5000

FINDINGS_SOURCE = "db-corruption"
_ROOT_CAUSE = "concurrent task_events writers (t_dfb4205b)"
# Run outcomes that mean "this suspect's worker died" — the crash-badge signal
# (see kanban_diagnostics._rule_repeated_crashes, task t_e5997536).
_CRASHED_OUTCOMES = frozenset({"crashed", "reclaimed", "timed_out"})

CLASS_INDEX_ONLY = "index-only"
CLASS_STRUCTURAL = "structural"

# Shared zeus ``findings`` schema — identical DDL across the finding producers
# (see hermes_cli.logwatcher / hermes_cli.regular_crons); IF NOT EXISTS so
# whichever emitter runs first creates it and the ``source`` column keeps them
# from colliding.
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


def classify(problems: list[str]) -> str:
    """Bucket ``integrity_check`` output into the two operator-facing classes.

    ``index-only`` (REINDEX-fixable index/table desync — the recurring
    concurrent-writer case) vs ``structural`` (page/b-tree damage that needs
    offline recovery). Delegates the index-only test to the single source of
    truth in :mod:`hermes_cli.kanban_db` so the classification can never drift
    from what the auto-repair actually treats as repairable.
    """
    from hermes_cli import kanban_db as kb

    return CLASS_INDEX_ONLY if kb._integrity_rows_are_index_only(problems) else CLASS_STRUCTURAL


def _last_tool_call(payload_json: Optional[str]) -> dict[str, Any]:
    """Extract the human-facing bits of one tool_call payload (title/kind/status)."""
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("title", "kind", "status"):
        value = payload.get(key)
        if value:
            out[key] = str(value)[:120]
    return out


def snapshot_suspects(
    conn: sqlite3.Connection,
    *,
    at: int,
    window: int = WINDOW_SECONDS,
    max_suspects: int = MAX_SUSPECTS,
) -> list[dict[str, Any]]:
    """Runs that wrote to ``task_events`` in ``[at-window, at+window]``, worst first.

    Each suspect is ``{task_id, run_id, worker_pid, run_status, run_outcome,
    crashed, events, first_at, last_at, last_tool_call}``. ``crashed`` is True
    when the run's outcome is a died-mid-run signal (the crash-badge link,
    t_e5997536). Best-effort: any sqlite error (e.g. the DB is structurally
    corrupt and unreadable) degrades to an empty list — an anonymous episode is
    still recorded, just without named culprits.
    """
    lo, hi = at - window, at + window
    try:
        rows = conn.execute(
            "SELECT task_id, run_id, kind, payload, created_at FROM task_events "
            "WHERE created_at BETWEEN ? AND ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (lo, hi, _MAX_WINDOW_ROWS),
        ).fetchall()
    except sqlite3.Error:
        return []

    # Group by run (falling back to the task id when a row carries no run_id, so
    # un-scoped writers still surface as their own suspect bucket).
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        run_id = r["run_id"] if _has_key(r, "run_id") else None
        task_id = r["task_id"]
        key = (run_id, task_id)
        g = groups.get(key)
        created = int(r["created_at"])
        if g is None:
            g = groups[key] = {
                "task_id": task_id,
                "run_id": int(run_id) if run_id is not None else None,
                "events": 0,
                "first_at": created,
                "last_at": created,
                "last_tool_call": {},
            }
        g["events"] += 1
        g["last_at"] = created
        if r["kind"] == "tool_call":
            g["last_tool_call"] = _last_tool_call(r["payload"])

    suspects = sorted(groups.values(), key=lambda g: g["last_at"], reverse=True)
    suspects = suspects[:max_suspects]
    for s in suspects:
        _attach_run_facts(conn, s)
    return suspects


def _has_key(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return False


def _attach_run_facts(conn: sqlite3.Connection, suspect: dict[str, Any]) -> None:
    """Fill worker_pid + run status/outcome (the crash-badge link) for a suspect."""
    suspect["worker_pid"] = None
    suspect["run_status"] = None
    suspect["run_outcome"] = None
    suspect["crashed"] = False
    run_id = suspect.get("run_id")
    if run_id is None:
        return
    try:
        row = conn.execute(
            "SELECT worker_pid, status, outcome FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return
    if row is None:
        return
    suspect["worker_pid"] = row["worker_pid"]
    suspect["run_status"] = row["status"]
    suspect["run_outcome"] = row["outcome"]
    suspect["crashed"] = (row["outcome"] or "") in _CRASHED_OUTCOMES


def _fmt_time(ts: int) -> str:
    """Local wall-clock ``HH:MM:SS DD.MM`` for the card (matches the UI locale)."""
    try:
        return time.strftime("%H:%M:%S %d.%m", time.localtime(ts))
    except (ValueError, OSError):
        return str(ts)


def _suspect_line(s: dict[str, Any]) -> str:
    tc = s.get("last_tool_call") or {}
    label = tc.get("title") or tc.get("kind") or "—"
    status = tc.get("status")
    tail = f" ({status})" if status else ""
    pid = s.get("worker_pid")
    pid_part = f", pid {pid}" if pid else ""
    run_id = s.get("run_id")
    run_part = f", run {run_id}" if run_id is not None else ""
    crashed = " ⚠ ран крашнулся (см. бейдж краша)" if s.get("crashed") else ""
    return (
        f"- `{s['task_id']}`{pid_part}{run_part} — последний tool_call: "
        f"«{label}»{tail} в {_fmt_time(int(s['last_at']))}{crashed}"
    )


def build_detail(
    *, corruption_class: str, at: int, path: str, problems: list[str],
    suspects: list[dict[str, Any]], healed: bool,
) -> str:
    """Compose the Проблема card body: time, class, culprits, crash cross-ref."""
    if corruption_class == CLASS_INDEX_ONLY:
        class_ru = "index-only (индексы пересобраны REINDEX, данные целы)"
    else:
        class_ru = "структурная (страничная порча, нужна ручная реставрация)"
    heal_ru = "самовосстановление прошло" if healed else "автопочинка невозможна"
    lines = [
        f"Порча БД kanban обнаружена {_fmt_time(at)}; {heal_ru}.",
        "",
        f"**Класс:** {class_ru}",
        f"**Файл:** `{path}`",
    ]
    if problems:
        joined = "; ".join(problems[:8])
        lines.append(f"**integrity_check:** {joined[:500]}")
    lines.append("")
    if suspects:
        lines.append(
            "**Подозреваемые задачи** (писали в task_events в окне "
            f"±{WINDOW_SECONDS}с вокруг детекта):"
        )
        lines.extend(_suspect_line(s) for s in suspects)
    else:
        lines.append(
            "**Аноним:** писателей в task_events в окне "
            f"±{WINDOW_SECONDS}с не найдено."
        )
    lines.append("")
    lines.append(
        "_Корреляция по времени, без LLM. Краши подозреваемых — см. бейджи "
        "крашей (t_e5997536)._"
    )
    return "\n".join(lines)


def _finding_key(at: int, path: str) -> str:
    """One card per episode: keyed by detection time + db path (stable on re-emit)."""
    return f"corruption:{Path(path).name}:{at}"


def build_finding(
    *, corruption_class: str, at: int, path: str, problems: list[str],
    suspects: list[dict[str, Any]], healed: bool,
) -> dict[str, Any]:
    """Render the emit-ready finding dict for one corruption episode."""
    severity = "error" if corruption_class == CLASS_INDEX_ONLY else "critical"
    named = [s for s in suspects if s.get("task_id")]
    crashed = [s for s in named if s.get("crashed")]
    if named:
        who = f"подозреваются {len(named)} задач(и)"
        if crashed:
            who += f", из них {len(crashed)} с крашем рана"
    else:
        who = "виновник не определён (аноним)"
    title = f"Порча БД kanban ({corruption_class}): {who}"
    detail = build_detail(
        corruption_class=corruption_class, at=at, path=path, problems=problems,
        suspects=suspects, healed=healed,
    )
    evidence = {
        "corruption_class": corruption_class,
        "detected_at": at,
        "path": path,
        "healed": healed,
        "problems": problems[:20],
        "root_cause": _ROOT_CAUSE,
        "suspects": suspects,
        "crashed_task_ids": [s["task_id"] for s in crashed],
    }
    return {
        "finding_key": _finding_key(at, path),
        "title": title,
        "detail": detail,
        "category": "db-corruption",
        "severity": severity,
        "evidence": evidence,
    }


def _open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection, or None if the ledger is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching the other finding producers.
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


def emit_episode_finding(
    *,
    corruption_class: str,
    at: int,
    path: str,
    problems: list[str],
    suspects: list[dict[str, Any]],
    healed: bool,
    board: str = "",
    conn: Optional[sqlite3.Connection] = None,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Upsert one corruption-episode draft card into the shared findings store.

    Best-effort: a ``None`` connection with no zeus.db is a no-op (no store → no
    card), and any sqlite failure is swallowed so forensics never breaks the DB
    repair. Returns the emitted finding dict, or ``None`` when nothing was
    written. ``conn`` may be injected (tests); otherwise a zeus.db connection is
    opened and closed here.
    """
    finding = build_finding(
        corruption_class=corruption_class, at=at, path=path, problems=problems,
        suspects=suspects, healed=healed,
    )
    own = conn is None
    if own:
        conn = _open_findings_db()
    if conn is None:
        return None
    now = time.time() if now is None else now
    try:
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
             finding["detail"], json.dumps(finding["evidence"], ensure_ascii=False),
             finding["category"], finding["severity"], now, now),
        )
        conn.commit()
        return finding
    except sqlite3.Error:
        return None
    finally:
        if own:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def record_episode(
    conn: sqlite3.Connection,
    *,
    event: str,
    severity: str,
    problems: list[str],
    path: str,
    at: Optional[int] = None,
    backup: Optional[str] = None,
    board: str = "",
) -> dict[str, Any]:
    """Full episode record for the HEALED path (connection proven healthy again).

    Correlates suspects from ``task_events``, enriches the ``self_heal``
    engine_log breadcrumb with the corruption class + suspect list, and pushes
    the Проблема card. Fully best-effort: every sink is guarded so a logging
    failure can never break the repair that just succeeded. Returns
    ``{at, corruption_class, suspects}``.
    """
    at = int(time.time()) if at is None else int(at)
    corruption_class = classify(problems)
    suspects = snapshot_suspects(conn, at=at)
    payload = {
        "path": path,
        "problems": problems[:20],
        "corruption_class": corruption_class,
        "suspects": suspects,
        "root_cause": _ROOT_CAUSE,
    }
    if backup is not None:
        payload["pre_repair_backup"] = backup
    try:
        from hermes_cli import kanban_db as kb

        kb.record_log(
            conn, source="operator", event=event, severity=severity,
            category="self_heal", payload=payload,
        )
    except Exception:
        pass
    emit_episode_finding(
        corruption_class=corruption_class, at=at, path=path, problems=problems,
        suspects=suspects, healed=True, board=board,
    )
    return {"at": at, "corruption_class": corruption_class, "suspects": suspects}
