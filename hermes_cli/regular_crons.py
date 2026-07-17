"""The "Регулярные" registry — a token-aware view over the cron job store.

Operator model (task t_cdbf72d2): a *scheduled* thing is a work card with a
date (kanban ``status=scheduled`` — fires, produces a deliverable, then leaves
the board); a *regular* thing is a standing system process with a rhythm (a
``cron/jobs.json`` entry — never "completes", toggles on/off, emits health and
findings rather than a deliverable). So every cron job *is* a regular process,
and this module classifies each along the two axes the registry sorts by:

* **cadence** — ``interval`` (pulse / polling, "каждые 5м") vs ``calendar``
  (``cron`` expression, "ежедневно 03:00");
* **purpose** — supervision/health, reflection, security, resources/pacing.

It layers per-cron token spend (from the zeus ledger via
:mod:`hermes_cli.zeus_tokens`) onto each row, and provides anomaly detection
(a cron failing repeatedly in a row, or a run burning tokens far above its own
baseline) that pushes findings to the zeus ``findings`` store — the pull view
(open the tab) stays quiet; only anomalies push. Everything degrades to a bare
registry when the ledger is absent.

Pure, dependency-injectable functions: the classifiers and detectors take plain
dicts/lists so they unit-test without a live store; the ``registry_for_jobs`` /
``scan_and_emit`` entry points wire the real cron output dir and zeus ledger.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import zeus_tokens

# --- Two-axis taxonomy ------------------------------------------------------

# Purpose axis. Ordered: the first bucket whose keywords hit the job's
# name/script/prompt wins, so the more specific categories are checked before
# the catch-all supervision bucket. Unmatched jobs fall through to "other".
PURPOSE_SECURITY = "security"
PURPOSE_REFLECTION = "reflection"
PURPOSE_RESOURCES = "resources"
PURPOSE_SUPERVISION = "supervision"
PURPOSE_OTHER = "other"

_PURPOSE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (PURPOSE_SECURITY, ("security", "secscan", "sec-review", "sec_review", "vuln", "audit")),
    (PURPOSE_REFLECTION, ("reflect", "retro", "self-review", "self_review", "postmortem")),
    (PURPOSE_RESOURCES,
     ("limit", "quota", "pacing", "budget", "replenish", "token", "rate-limit", "cost")),
    (PURPOSE_SUPERVISION,
     ("health", "heartbeat", "watchdog", "monitor", "tick", "e2e", "backup",
      "supervis", "probe", "healthcheck")),
)

# Human (RU) labels — the tab is "brand-as-config", so these live in one place.
PURPOSE_LABELS: dict[str, str] = {
    PURPOSE_SUPERVISION: "Надзор / здоровье",
    PURPOSE_REFLECTION: "Рефлексия",
    PURPOSE_SECURITY: "Безопасность",
    PURPOSE_RESOURCES: "Ресурсы / пейсинг",
    PURPOSE_OTHER: "Прочее",
}

CADENCE_INTERVAL = "interval"
CADENCE_CALENDAR = "calendar"
CADENCE_ONCE = "once"
CADENCE_UNKNOWN = "unknown"

CADENCE_LABELS: dict[str, str] = {
    CADENCE_INTERVAL: "Интервальные",
    CADENCE_CALENDAR: "Календарные",
    CADENCE_ONCE: "Однократные",
    CADENCE_UNKNOWN: "Прочие",
}

# Anomaly thresholds. Deliberately conservative so the push path stays rare.
DEFAULT_FAILURE_STREAK = 3          # this many failures in a row -> finding
DEFAULT_TOKEN_SPIKE_FACTOR = 3.0    # latest run >= factor x baseline -> finding
DEFAULT_TOKEN_SPIKE_MIN_HISTORY = 3  # need this many prior runs for a baseline
FINDINGS_SOURCE = "regular-crons"


# --- Classification (pure) --------------------------------------------------


def _job_text(job: dict[str, Any]) -> str:
    """Lower-cased haystack for purpose keyword matching."""
    parts = [str(job.get(k) or "") for k in ("name", "script", "prompt")]
    return " ".join(parts).lower()


def classify_purpose(job: dict[str, Any]) -> str:
    """Map a cron job to one purpose bucket (see ``_PURPOSE_RULES``)."""
    hay = _job_text(job)
    for purpose, keywords in _PURPOSE_RULES:
        if any(kw in hay for kw in keywords):
            return purpose
    return PURPOSE_OTHER


def classify_cadence(job: dict[str, Any]) -> str:
    """Cadence axis from ``schedule.kind`` (interval / cron / once)."""
    kind = str((job.get("schedule") or {}).get("kind") or "").lower()
    if kind == "interval":
        return CADENCE_INTERVAL
    if kind == "cron":
        return CADENCE_CALENDAR
    if kind == "once":
        return CADENCE_ONCE
    return CADENCE_UNKNOWN


_DAILY_CRON = re.compile(r"^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$")


def cadence_badge(job: dict[str, Any]) -> str:
    """Compact human cadence label, e.g. "каждые 5м" or "ежедневно 03:00".

    Falls back to the job's own ``schedule_display`` (or raw cron expr) for
    shapes we don't special-case, so nothing is ever mislabelled — an unusual
    schedule just shows verbatim.
    """
    sched = job.get("schedule") or {}
    kind = classify_cadence(job)
    if kind == CADENCE_INTERVAL:
        minutes = int(sched.get("minutes") or 0)
        if minutes and minutes % 60 == 0:
            return f"каждые {minutes // 60}ч"
        return f"каждые {minutes}м" if minutes else "интервал"
    if kind == CADENCE_CALENDAR:
        expr = str(sched.get("expr") or "")
        m = _DAILY_CRON.match(expr)
        if m:
            minute, hour = int(m.group(1)), int(m.group(2))
            return f"ежедневно {hour:02d}:{minute:02d}"
    return str(job.get("schedule_display") or sched.get("display") or sched.get("expr") or "")


# --- Anomaly detection (pure) -----------------------------------------------


def detect_failure_streak(outcomes: list[str], threshold: int = DEFAULT_FAILURE_STREAK) -> int:
    """Length of the leading run of ``"error"`` outcomes, else 0.

    ``outcomes`` is newest-first (as :func:`recent_run_outcomes` returns). The
    streak is reported only once it reaches ``threshold``, so a single flake
    never trips a finding.
    """
    streak = 0
    for outcome in outcomes:
        if outcome == "error":
            streak += 1
        else:
            break
    return streak if streak >= threshold else 0


def detect_token_spike(
    per_run_totals: list[int],
    factor: float = DEFAULT_TOKEN_SPIKE_FACTOR,
    min_history: int = DEFAULT_TOKEN_SPIKE_MIN_HISTORY,
) -> Optional[dict[str, Any]]:
    """Flag the latest run if it burned ``factor``x the median of prior runs.

    ``per_run_totals`` is oldest-first. Needs at least ``min_history`` prior
    runs (a stable baseline) plus the latest. Returns
    ``{latest, baseline, factor}`` or ``None``.
    """
    if len(per_run_totals) < min_history + 1:
        return None
    *prior, latest = per_run_totals
    baseline = statistics.median(prior)
    if baseline > 0 and latest >= factor * baseline:
        return {"latest": int(latest), "baseline": float(baseline),
                "factor": round(latest / baseline, 1)}
    return None


# --- Run history (reads the cron output dir) --------------------------------


def _outcome_from_output_file(path: Path) -> str:
    """Classify one cron run-output ``.md`` as ``"error"`` or ``"ok"``.

    Agent runs write a ``# Cron Job: <name> (FAILED)`` header and an
    ``## Error`` section on failure; scripts and silent runs don't. We treat
    either marker as a failure and everything else as success.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "ok"
    head = text[:4000]
    if "(FAILED)" in head or "\n## Error" in head:
        return "error"
    return "ok"


def recent_run_outcomes(job_id: str, output_dir: Path, limit: int = 10) -> list[str]:
    """Outcomes of a cron's most recent runs, newest first.

    Reads ``<output_dir>/<job_id>/*.md`` (one file per run, timestamp-named, so
    a lexical sort is chronological). Returns ``[]`` when the job never ran.
    """
    job_dir = output_dir / job_id
    if not job_dir.is_dir():
        return []
    files = sorted((p for p in job_dir.glob("*.md")), reverse=True)[:limit]
    return [_outcome_from_output_file(p) for p in files]


# --- Row assembly -----------------------------------------------------------


def build_row(
    job: dict[str, Any],
    *,
    token_stats: Optional[dict[str, Any]] = None,
    period_days: Optional[int] = None,
    failure_streak: int = 0,
    token_spike: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Shape one registry row from a cron job dict plus its enrichments."""
    purpose = classify_purpose(job)
    cadence = classify_cadence(job)
    anomalies: list[dict[str, Any]] = []
    if failure_streak:
        anomalies.append({"kind": "consecutive_failures", "streak": failure_streak})
    if token_spike:
        anomalies.append({"kind": "token_spike", **token_spike})
    tokens = None
    if token_stats:
        tokens = {**token_stats, "period_days": period_days,
                  "display": zeus_tokens.humanize_tokens(int(token_stats.get("total_tokens") or 0))}
    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "profile": job.get("profile") or job.get("profile_name"),
        "purpose": purpose,
        "purpose_label": PURPOSE_LABELS[purpose],
        "cadence": cadence,
        "cadence_label": CADENCE_LABELS[cadence],
        "cadence_badge": cadence_badge(job),
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state"),
        "no_agent": bool(job.get("no_agent")),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_delivery_error": job.get("last_delivery_error"),
        "next_run_at": job.get("next_run_at"),
        "tokens": tokens,
        "anomalies": anomalies,
    }


def registry_for_jobs(
    jobs: Iterable[dict[str, Any]],
    *,
    zeus_conn: Optional[sqlite3.Connection] = None,
    output_dir: Optional[Path] = None,
    period_days: int = 30,
    now: Optional[float] = None,
    with_anomalies: bool = True,
) -> list[dict[str, Any]]:
    """Build the full registry: one enriched row per cron job.

    Token spend is read from ``zeus_conn`` over the trailing ``period_days``
    window. When ``with_anomalies`` is set, each row also carries any live
    anomaly (failure streak / token spike); ``output_dir`` supplies the run
    history for the streak check (defaults to the active cron output dir).
    """
    jobs = list(jobs)
    now = time.time() if now is None else now
    since_ts = now - period_days * 86400
    ids = [str(j.get("id")) for j in jobs if j.get("id")]
    token_map = zeus_tokens.aggregate_by_cron(zeus_conn, ids, since_ts=since_ts)
    if output_dir is None:
        output_dir = _default_output_dir()
    rows: list[dict[str, Any]] = []
    for job in jobs:
        jid = str(job.get("id") or "")
        streak = 0
        spike = None
        if with_anomalies and jid:
            streak = detect_failure_streak(recent_run_outcomes(jid, output_dir))
            spike = detect_token_spike(
                zeus_tokens.per_run_token_totals(zeus_conn, jid, since_ts=since_ts)
            )
        rows.append(build_row(job, token_stats=token_map.get(jid),
                              period_days=period_days, failure_streak=streak,
                              token_spike=spike))
    return rows


def _default_output_dir() -> Path:
    from cron import jobs as cron_jobs
    return cron_jobs.get_cron_output_dir()


# --- Findings push ----------------------------------------------------------

# Mirrors the zeus ``findings`` store schema; ``IF NOT EXISTS`` so we degrade to
# creating it when the zeus.db exists but the external plugin never made it.
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


def _finding_key(job_id: str, kind: str) -> str:
    return f"cron:{job_id}:{kind}"


def emit_finding(
    conn: sqlite3.Connection,
    *,
    board: str,
    finding_key: str,
    title: str,
    detail: str,
    category: str,
    severity: str,
    evidence: Any,
    now: Optional[float] = None,
) -> None:
    """Upsert one open finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes the title/detail/severity and ``updated_at`` but
    preserves ``created_at`` and never un-dismisses a finding a human already
    put to rest (dismissed/snoozed stay as-is).
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
        "  status=CASE WHEN findings.status IN ('dismissed','snoozed') "
        "              THEN findings.status ELSE 'open' END",
        (board, FINDINGS_SOURCE, finding_key, title, detail,
         json.dumps(evidence), category, severity, now, now),
    )
    conn.commit()


def clear_finding(conn: sqlite3.Connection, *, board: str, finding_key: str,
                  now: Optional[float] = None) -> None:
    """Mark a previously-open regular-cron finding obsolete (anomaly resolved)."""
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
    rows: Iterable[dict[str, Any]],
    conn: Optional[sqlite3.Connection],
    *,
    board: str = "",
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Push a finding per anomalous row and clear findings that recovered.

    Idempotent: run it every health-check tick. Returns the findings emitted
    this pass (for logging / the CLI). A ``None`` connection (no zeus ledger)
    is a no-op — the registry is still readable, just without the push path.
    """
    rows = list(rows)
    if conn is None:
        return []
    emitted: list[dict[str, Any]] = []
    for row in rows:
        jid = str(row.get("id") or "")
        if not jid:
            continue
        kinds = {a["kind"] for a in row.get("anomalies") or []}
        for anomaly in row.get("anomalies") or []:
            finding = _finding_for(row, anomaly, board=board, now=now)
            emit_finding(conn, board=board, **finding["emit"])
            emitted.append(finding["summary"])
        for kind in ("consecutive_failures", "token_spike"):
            if kind not in kinds:
                clear_finding(conn, board=board,
                              finding_key=_finding_key(jid, kind), now=now)
    return emitted


def _finding_for(row: dict[str, Any], anomaly: dict[str, Any], *,
                 board: str, now: Optional[float]) -> dict[str, Any]:
    """Render the emit-kwargs + a log summary for one anomaly on one row."""
    jid = str(row.get("id"))
    name = row.get("name") or jid
    kind = anomaly["kind"]
    if kind == "consecutive_failures":
        n = anomaly["streak"]
        title = f"Регулярный процесс «{name}» падает подряд ({n})"
        detail = (f"Крон {jid} завершился ошибкой {n} раз(а) подряд. "
                  f"Последняя ошибка: {row.get('last_error') or 'n/a'}")
        category, severity = "reliability", "warning"
    else:
        f = anomaly["factor"]
        title = f"Всплеск токенов у «{name}» (x{f})"
        detail = (f"Последний запуск крона {jid} израсходовал "
                  f"{anomaly['latest']} токенов при базлайне "
                  f"~{int(anomaly['baseline'])} (x{f}).")
        category, severity = "cost", "warning"
    return {
        "emit": {
            "finding_key": _finding_key(jid, kind),
            "title": title, "detail": detail, "category": category,
            "severity": severity, "evidence": anomaly, "now": now,
        },
        "summary": {"id": jid, "kind": kind, "title": title, "severity": severity},
    }


# --- Convenience entry points ------------------------------------------------


def default_findings_db_path() -> Path:
    """Findings live in the same zeus.db as the token ledger."""
    return zeus_tokens.default_zeus_db_path()


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings, or ``None`` if the file is absent.

    Unlike :func:`zeus_tokens.connect` (read-only) this can write; we still
    decline to create the database file from scratch, so a host without the
    zeus ledger simply has no push path.
    """
    path = default_findings_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def ticker_health() -> dict[str, Any]:
    """Overall subsystem-health header for the registry (the cron ticker itself).

    ``heartbeat_age`` bumps every tick; ``success_age`` only when a tick fires
    jobs cleanly. Stale heartbeat -> the whole regular-cron system is down.
    """
    from cron import jobs as cron_jobs
    return {
        "heartbeat_age": cron_jobs.get_ticker_heartbeat_age(),
        "success_age": cron_jobs.get_ticker_success_age(),
        "interval_seconds": getattr(cron_jobs, "TICKER_INTERVAL_SECONDS", 60),
    }
