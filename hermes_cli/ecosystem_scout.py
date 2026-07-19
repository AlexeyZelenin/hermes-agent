"""Ecosystem scout — regular scan of fresh tooling + effectiveness verdict.

Operator model (task t_bad68065): a weekly two-card loop over the agentic-dev
ecosystem, extending the regular-process family (see
:mod:`hermes_cli.regular_crons`, :mod:`hermes_cli.vision_reconcile`,
:mod:`hermes_cli.subscription_limits`).

* the SCAN card (agent cron) sweeps fresh releases — Claude Code plugins, MCP
  servers, skills, agent tooling — filters to our stack (Hermes/Zeus, macOS,
  fish) and drops ONE triage card with a "стоит поставить" short-list. It
  proposes; it never installs.
* the VERDICT card (this script cron) measures effect AFTER an operator installs
  something: for each tracked intervention it compares task-efficiency metrics
  (tokens/task, retry share, time-to-done) in the window BEFORE the install
  against the window AFTER, and pushes a "помогло / нейтрально / убрать" verdict
  to the shared zeus ``findings`` store.

The metric math is pure and dependency-injectable (it operates on plain
``TaskMetric`` dicts), so it unit-tests without a live ledger; the loaders read
the zeus token ledger (tokens) and the kanban DB (lifecycle + attempts) and
degrade to an empty scan when either is absent. Findings are tagged
``source=ecosystem-scout`` and carry an explicit "наблюдение, не доказательство
причинности" caveat — a before/after window comparison is correlation, not
proof: other factors can move within the same window.
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FINDINGS_SOURCE = "ecosystem-scout"

# A tracked install we measure the effect of. ``installed_at`` is epoch seconds.
# ``window_days`` sets the symmetric before/after comparison window.
DEFAULT_WINDOW_DAYS = 14
# Need at least this many done tasks in each window before a verdict is credible;
# below it we stay silent rather than read noise as signal.
MIN_TASKS_PER_WINDOW = 5
# A metric must move by this fraction to count as improved/worsened — smaller
# swings are treated as noise so the verdict does not flip on run-to-run jitter.
IMPROVE_THRESHOLD = 0.10

_SECONDS_PER_DAY = 86400

# Verdict labels. Stable keys — they render into the finding, not the finding_key.
VERDICT_HELPED = "helped"
VERDICT_HURT = "hurt"
VERDICT_NEUTRAL = "neutral"
VERDICT_INSUFFICIENT = "insufficient_data"

VERDICT_LABELS: dict[str, str] = {
    VERDICT_HELPED: "похоже, помогло",
    VERDICT_HURT: "похоже, мешает — кандидат на удаление",
    VERDICT_NEUTRAL: "нейтрально",
    VERDICT_INSUFFICIENT: "недостаточно данных",
}

# Delta key -> the window_stats field it compares. All three metrics are
# "lower is better", so a negative delta is an improvement.
_DELTA_KEYS: dict[str, str] = {
    "tokens": "median_tokens",
    "retry_share": "retry_share",
    "duration": "median_duration_s",
}


# --- Metric math (pure) -----------------------------------------------------


def window_stats(metrics: Iterable[dict[str, Any]], start_ts: float,
                 end_ts: float) -> dict[str, Any]:
    """Aggregate task metrics for done-tasks in the ``[start_ts, end_ts)`` window.

    ``retry_share`` is over all tasks in the window; the medians ignore tasks
    with no ledger tokens / no measured duration (a zero there means "missing
    data", not "free / instant"), so a partial ledger does not drag them to 0.
    """
    sel = [m for m in metrics if start_ts <= float(m.get("done_ts") or 0) < end_ts]
    n = len(sel)
    tokens = [int(m["tokens"]) for m in sel if m.get("tokens")]
    durations = [float(m["duration_s"]) for m in sel if m.get("duration_s")]
    retried = sum(1 for m in sel if int(m.get("attempts") or 1) > 1)
    return {
        "n": n,
        "median_tokens": statistics.median(tokens) if tokens else None,
        "retry_share": (retried / n) if n else None,
        "median_duration_s": statistics.median(durations) if durations else None,
    }


def _pct_change(baseline: Optional[float], after: Optional[float]) -> Optional[float]:
    """Signed fractional change baseline→after, or ``None`` without a baseline."""
    if baseline is None or after is None or baseline == 0:
        return None
    return (after - baseline) / baseline


def classify_verdict(baseline: dict[str, Any], after: dict[str, Any],
                     deltas: dict[str, Optional[float]]) -> str:
    """Roll the three metric deltas into one verdict (all lower-is-better)."""
    if (baseline.get("n") or 0) < MIN_TASKS_PER_WINDOW:
        return VERDICT_INSUFFICIENT
    if (after.get("n") or 0) < MIN_TASKS_PER_WINDOW:
        return VERDICT_INSUFFICIENT
    improved = sum(1 for d in deltas.values() if d is not None and d <= -IMPROVE_THRESHOLD)
    worsened = sum(1 for d in deltas.values() if d is not None and d >= IMPROVE_THRESHOLD)
    if improved >= 2 and improved > worsened:
        return VERDICT_HELPED
    if worsened >= 2 and worsened > improved:
        return VERDICT_HURT
    return VERDICT_NEUTRAL


def compare(metrics: Iterable[dict[str, Any]], install_ts: float,
            window_days: int = DEFAULT_WINDOW_DAYS, *,
            now: Optional[float] = None) -> dict[str, Any]:
    """Compare the ``window_days`` before an install against the window after.

    The after-window is capped at ``now`` so a freshly-registered intervention
    reports on the data it actually has rather than an empty future window.
    """
    metrics = list(metrics)
    now = time.time() if now is None else now
    span = window_days * _SECONDS_PER_DAY
    baseline = window_stats(metrics, install_ts - span, install_ts)
    after = window_stats(metrics, install_ts, min(install_ts + span, now))
    deltas = {name: _pct_change(baseline[field], after[field])
              for name, field in _DELTA_KEYS.items()}
    return {"baseline": baseline, "after": after, "deltas": deltas,
            "window_days": window_days,
            "verdict": classify_verdict(baseline, after, deltas)}


# --- Interventions registry -------------------------------------------------


def default_interventions_path() -> Path:
    """The tracked-installs registry, under ``HERMES_HOME/ecosystem_scout``."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "ecosystem_scout" / "interventions.json"


def load_interventions(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read the tracked interventions; ``[]`` when the registry is absent/broken."""
    path = path or default_interventions_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("interventions") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict) and i.get("name")] \
        if isinstance(items, list) else []


def save_interventions(items: list[dict[str, Any]],
                       path: Optional[Path] = None) -> None:
    """Persist the interventions registry (creates the parent dir)."""
    path = path or default_interventions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"interventions": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def add_intervention(name: str, installed_at: float, *,
                     window_days: int = DEFAULT_WINDOW_DAYS, note: str = "",
                     path: Optional[Path] = None) -> dict[str, Any]:
    """Register (or update, keyed by ``name``) a tracked install and persist it."""
    entry = {"name": name, "installed_at": float(installed_at),
             "window_days": int(window_days), "note": note}
    items = [i for i in load_interventions(path) if i.get("name") != name]
    items.append(entry)
    save_interventions(items, path)
    return entry


# --- Loaders (read zeus ledger + kanban DB) ---------------------------------


def _load_lifecycle(conn: sqlite3.Connection,
                    since_ts: Optional[float]) -> list[dict[str, Any]]:
    """Done-task lifecycle rows (id, done_ts, duration_s) from the kanban DB."""
    query = ("SELECT id, started_at, completed_at FROM tasks "
             "WHERE status='done' AND completed_at IS NOT NULL")
    params: tuple = ()
    if since_ts is not None:
        query += " AND completed_at >= ?"
        params = (int(since_ts),)
    rows = conn.execute(query, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        completed = float(row["completed_at"])
        started = row["started_at"]
        duration = (completed - float(started)) if started else None
        out.append({"task_id": row["id"], "done_ts": completed,
                    "duration_s": duration if duration and duration > 0 else None})
    return out


def _load_attempts(conn: sqlite3.Connection) -> dict[str, int]:
    """Attempt count per task (``>1`` means the task was retried)."""
    try:
        rows = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_runs GROUP BY task_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["task_id"]: int(row["n"]) for row in rows}


def load_task_metrics(board: Optional[str] = None,
                      since_ts: Optional[float] = None) -> list[dict[str, Any]]:
    """Assemble per-task metrics from the kanban DB (lifecycle/attempts) + zeus
    ledger (tokens). Degrades to ``[]`` when the kanban DB is unavailable."""
    try:
        from hermes_cli import kanban_db
        conn = kanban_db.connect(board=board)
    except Exception:
        return []
    try:
        lifecycle = _load_lifecycle(conn, since_ts)
        attempts = _load_attempts(conn)
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    tokens = _load_tokens([m["task_id"] for m in lifecycle])
    for m in lifecycle:
        m["attempts"] = attempts.get(m["task_id"], 1)
        m["tokens"] = tokens.get(m["task_id"], 0)
    return lifecycle


def _load_tokens(task_ids: list[str]) -> dict[str, int]:
    """Total tokens per task from the zeus ledger; ``{}`` when it is absent."""
    try:
        from hermes_cli import zeus_tokens
        conn = zeus_tokens.connect()
    except Exception:
        return {}
    if conn is None:
        return {}
    try:
        agg = zeus_tokens.aggregate_by_task(conn, task_ids)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {tid: int(v.get("total_tokens") or 0) for tid, v in agg.items()}


# --- Findings store (shared zeus.db, source=ecosystem-scout) -----------------
#
# Mirrors the shared zeus ``findings`` schema (see hermes_cli.vision_reconcile /
# regular_crons / subscription_limits). Same DDL, IF NOT EXISTS so whichever
# regular process runs first creates it; distinguished by source.
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


def finding_key(name: str) -> str:
    """Stable per-intervention key so re-running upserts one verdict, not many."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"
    return f"scout:verdict:{slug}"


def emit_finding(conn: sqlite3.Connection, finding: dict[str, Any], *,
                 board: str, now: Optional[float] = None) -> None:
    """Upsert one open verdict finding, keyed by ``(board, source, finding_key)``.

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
         finding["detail"], json.dumps(finding.get("evidence"), ensure_ascii=False),
         finding.get("category", ""), finding.get("severity", "info"), now, now),
    )
    conn.commit()


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings, or ``None`` if the file is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching :func:`hermes_cli.vision_reconcile.open_findings_db`.
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


# --- Finding rendering ------------------------------------------------------


def _fmt_pct(delta: Optional[float]) -> str:
    return "n/a" if delta is None else f"{delta * 100:+.0f}%"


def _fmt_tokens(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{int(value):,}".replace(",", " ")


def _fmt_minutes(seconds: Optional[float]) -> str:
    return "n/a" if seconds is None else f"{seconds / 60:.0f}м"


def _fmt_share(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _detail_lines(name: str, result: dict[str, Any], installed_at: float) -> list[str]:
    base, after, deltas = result["baseline"], result["after"], result["deltas"]
    installed = time.strftime("%Y-%m-%d", time.gmtime(installed_at))
    return [
        f"Наблюдение по метрикам задач вокруг установки «{name}» "
        f"(установлено {installed}, окно ±{result['window_days']}д; "
        f"задач до={base['n']}, после={after['n']}).",
        f"- Токены/задача (медиана): {_fmt_tokens(base['median_tokens'])} → "
        f"{_fmt_tokens(after['median_tokens'])} ({_fmt_pct(deltas['tokens'])}).",
        f"- Доля ретраев: {_fmt_share(base['retry_share'])} → "
        f"{_fmt_share(after['retry_share'])} ({_fmt_pct(deltas['retry_share'])}).",
        f"- Время до done (медиана): {_fmt_minutes(base['median_duration_s'])} → "
        f"{_fmt_minutes(after['median_duration_s'])} ({_fmt_pct(deltas['duration'])}).",
        "Меньше — лучше по всем трём. Это наблюдение (корреляция), не "
        "доказательство причинности: в окне могли меняться и другие факторы.",
    ]


def _finding_for(intervention: dict[str, Any],
                 result: dict[str, Any]) -> dict[str, Any]:
    """Render the emit-ready finding dict for one intervention's verdict."""
    name = str(intervention.get("name") or "")
    verdict = result["verdict"]
    label = VERDICT_LABELS.get(verdict, verdict)
    severity = "warning" if verdict == VERDICT_HURT else "info"
    return {
        "finding_key": finding_key(name),
        "title": f"Вердикт по «{name}»: {label}",
        "detail": "\n".join(
            _detail_lines(name, result, float(intervention.get("installed_at") or 0))),
        "category": "ecosystem-effect",
        "severity": severity,
        "evidence": {"verdict": verdict, "deltas": result["deltas"],
                     "baseline": result["baseline"], "after": result["after"]},
    }


# --- Verdict runner ---------------------------------------------------------


def run_verdict(*, board: Optional[str] = None,
                interventions: Optional[list[dict[str, Any]]] = None,
                metrics: Optional[list[dict[str, Any]]] = None,
                conn: Optional[sqlite3.Connection] = None,
                emit: bool = True, now: Optional[float] = None
                ) -> list[dict[str, Any]]:
    """Compute (and, when ``emit``, push) a verdict per tracked intervention.

    Loads task metrics once over the widest window any intervention needs. Only
    verdicts with enough data on both sides push a finding — an intervention
    still inside its first window stays quiet. Returns one result dict per
    intervention (verdict + windows + deltas) regardless of emit.
    """
    now = time.time() if now is None else now
    items = interventions if interventions is not None else load_interventions()
    if not items:
        return []
    if metrics is None:
        earliest = min(
            float(i.get("installed_at") or now)
            - int(i.get("window_days") or DEFAULT_WINDOW_DAYS) * _SECONDS_PER_DAY
            for i in items)
        metrics = load_task_metrics(board=board, since_ts=earliest)
    results = [{"intervention": it,
                **compare(metrics, float(it.get("installed_at") or now),
                          int(it.get("window_days") or DEFAULT_WINDOW_DAYS), now=now)}
               for it in items]
    if emit:
        _emit_results(results, board=board or "", conn=conn, now=now)
    return results


def _emit_results(results: list[dict[str, Any]], *, board: str,
                  conn: Optional[sqlite3.Connection], now: float) -> None:
    """Push a finding for every result that reached a real verdict."""
    own_conn = conn is None
    if own_conn:
        conn = open_findings_db()
    if conn is None:
        return
    try:
        for result in results:
            if result["verdict"] == VERDICT_INSUFFICIENT:
                continue
            emit_finding(conn, _finding_for(result["intervention"], result),
                         board=board, now=now)
    finally:
        if own_conn:
            conn.close()


# --- Regular cron seeding ---------------------------------------------------

VERDICT_JOB_ORIGIN = {"kind": "ecosystem-scout-verdict"}
SCAN_JOB_ORIGIN = {"kind": "ecosystem-scout-scan"}

_RUNNER_SCRIPT_NAME = "ecosystem_scout_verdict_cron.py"
_RUNNER_SCRIPT_BODY = (
    "# Auto-generated by hermes_cli.ecosystem_scout — effectiveness verdict\n"
    "# cron runner (task t_bad68065). Managed by the regular-process seeder.\n"
    "from hermes_cli.ecosystem_scout import main\n"
    "raise SystemExit(main())\n"
)
_VERDICT_SCHEDULE = "0 5 * * 1"   # Mondays 05:00 (off-peak, weekly)
_SCAN_SCHEDULE = "0 9 * * 1"      # Mondays 09:00 (weekly, before the workday)

# The scan card's whole payload lives in the cron prompt: the fired agent turns
# it into ONE triage card and stops. It proposes; it never installs.
_SCAN_PROMPT = (
    "Регулярный скаут экосистемы агентной разработки (task t_bad68065). "
    "Просканируй СВЕЖИЕ релизы за последнюю неделю: плагины Claude Code "
    "(anthropics/claude-plugins-official + marketplace), MCP-серверы "
    "(modelcontextprotocol/servers, GitHub search по окну), skills, инструменты "
    "агентной разработки (release notes, awesome-списки, HN, GitHub Trending). "
    "Отфильтруй под наш стек: Hermes/Zeus (мульти-агентный kanban на macOS, "
    "shell fish, Python), приоритет — экономия токенов на задачу, надёжность "
    "воркеров, ревью-качество. Для каждого кандидата дай источник (URL) и одну "
    "строку обоснования «зачем нам». НИЧЕГО НЕ СТАВЬ. Создай РОВНО ОДНУ карточку "
    "через kanban_create с triage=true, assignee='default', board='ra', "
    "title='Скаут экосистемы: шорт-лист «стоит поставить» (<дата>)' и телом — "
    "коротким шорт-листом (3-7 пунктов) с источниками и обоснованиями, плюс "
    "явной пометкой, что это предложения на решение оператора, а не установка. "
    "Пиши тело обычным UTF-8 текстом без бинарного мусора. Больше в этой сессии "
    "ничего не делай — только создай карточку."
)


def _write_runner_script() -> Optional[str]:
    """Write the thin verdict runner into ``HERMES_HOME/scripts``; return its name."""
    try:
        from hermes_constants import get_hermes_home
        scripts_dir = get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / _RUNNER_SCRIPT_NAME).write_text(
            _RUNNER_SCRIPT_BODY, encoding="utf-8")
        return _RUNNER_SCRIPT_NAME
    except Exception as exc:
        logger.debug("could not write ecosystem-scout runner: %s", exc)
        return None


def _job_with_origin(kind: str) -> Optional[dict[str, Any]]:
    """Return an already-registered cron with this origin kind, else ``None``."""
    from cron import jobs as cron_jobs
    for job in cron_jobs.load_jobs():
        if (job.get("origin") or {}).get("kind") == kind:
            return job
    return None


def ensure_scout_verdict_job(*, schedule: str = _VERDICT_SCHEDULE
                             ) -> Optional[dict[str, Any]]:
    """Idempotently register the weekly effectiveness-verdict cron (no_agent).

    Safe to call on every boot/tick — an already-registered job short-circuits.
    Returns the existing or newly created job, or ``None`` when the cron store
    is unavailable.
    """
    try:
        existing = _job_with_origin(VERDICT_JOB_ORIGIN["kind"])
        if existing is not None:
            return existing
        script = _write_runner_script()
        if script is None:
            return None
        from cron import jobs as cron_jobs
        return cron_jobs.create_job(
            prompt=None, schedule=schedule,
            name="Ecosystem scout: вердикт эффективности установок",
            script=script, no_agent=True, deliver="local",
            origin=dict(VERDICT_JOB_ORIGIN))
    except Exception as exc:
        logger.debug("could not ensure ecosystem-scout verdict cron: %s", exc)
        return None


def ensure_scout_scan_job(*, schedule: str = _SCAN_SCHEDULE
                          ) -> Optional[dict[str, Any]]:
    """Idempotently register the weekly ecosystem-scan cron (agent → triage card).

    The fired agent gets web + kanban tools and creates ONE triage card. Safe to
    call on every boot/tick. Returns the existing or newly created job, or
    ``None`` when the cron store is unavailable.
    """
    try:
        existing = _job_with_origin(SCAN_JOB_ORIGIN["kind"])
        if existing is not None:
            return existing
        from cron import jobs as cron_jobs
        return cron_jobs.create_job(
            prompt=_SCAN_PROMPT, schedule=schedule,
            name="Ecosystem scout: скан свежих плагинов/MCP/тулзов",
            no_agent=False, deliver="local",
            enabled_toolsets=["web", "search", "kanban"],
            origin=dict(SCAN_JOB_ORIGIN))
    except Exception as exc:
        logger.debug("could not ensure ecosystem-scout scan cron: %s", exc)
        return None


# --- CLI --------------------------------------------------------------------


def _render_human(results: list[dict[str, Any]]) -> str:
    if not results:
        return ("Ecosystem scout: нет отслеживаемых установок "
                "(добавь через --add-intervention).")
    lines = [f"Ecosystem scout: вердикт по {len(results)} установке(ам):"]
    for r in results:
        name = r["intervention"].get("name")
        label = VERDICT_LABELS.get(r["verdict"], r["verdict"])
        lines.append(f"  [{r['verdict']}] «{name}»: {label} "
                     f"(до={r['baseline']['n']}, после={r['after']['n']})")
    return "\n".join(lines)


def _parse_installed_at(raw: Optional[str], now: float) -> float:
    """Accept an epoch (int/float) or a UTC ``YYYY-MM-DD``; default to ``now``.

    Dates are parsed as UTC (``calendar.timegm``) to match the UTC ``gmtime``
    rendering in the finding, so the stored and displayed day always agree.
    """
    if not raw:
        return now
    try:
        return float(raw)
    except ValueError:
        return float(calendar.timegm(time.strptime(raw, "%Y-%m-%d")))


def _cmd_add(args: argparse.Namespace) -> int:
    entry = add_intervention(
        args.add_intervention,
        _parse_installed_at(args.installed_at, time.time()),
        window_days=args.window_days, note=args.note or "")
    print(f"Ecosystem scout: отслеживаю «{entry['name']}» "
          f"(установлено {time.strftime('%Y-%m-%d', time.gmtime(entry['installed_at']))}, "
          f"окно ±{entry['window_days']}д).")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.ecosystem_scout`` — the verdict regular entry."""
    parser = argparse.ArgumentParser(
        prog="ecosystem-scout",
        description="Weekly effectiveness verdict for installed ecosystem tools.")
    parser.add_argument("--board", default=None, help="Kanban board slug.")
    parser.add_argument("--json", action="store_true", help="Emit results as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute verdicts only; do not push findings.")
    parser.add_argument("--list", action="store_true",
                        help="List tracked interventions and exit.")
    parser.add_argument("--add-intervention", metavar="NAME",
                        help="Register an installed tool to measure, then exit.")
    parser.add_argument("--installed-at", metavar="WHEN",
                        help="Install time (epoch or YYYY-MM-DD; default now).")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help="Before/after comparison window in days.")
    parser.add_argument("--note", default="", help="Freeform note for the install.")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(load_interventions(), ensure_ascii=False, indent=2))
        return 0
    if args.add_intervention:
        return _cmd_add(args)

    results = run_verdict(board=args.board, emit=not args.no_emit)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        print(_render_human(results))
    return 1 if any(r["verdict"] == VERDICT_HURT for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
