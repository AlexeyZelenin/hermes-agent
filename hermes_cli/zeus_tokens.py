"""Read-only view over the zeus token ledger (``~/.hermes/zeus/zeus.db``).

The per-task token/cost ledger is written by the external *zeus* plugin's
``post_api_request`` hook (see ``agent/acp_task_executor.py``); every API turn
lands a ``token_usage`` row tagged with the originating ``task_id`` — including
the reflection/review turns a card accrues, since those run under the same
``task_id``. This module gives the kanban dashboard and CLI a read-only "what
did this card cost to build" view, aggregated per ``task_id`` and rolled up over
an epic's sub-tasks (the ROI view).

Everything here degrades to nothing when the ledger is absent (zeus plugin not
installed): :func:`connect` returns ``None`` and the aggregators return ``{}`` /
``None`` rather than raising. The ledger lives in a *separate* database from
``kanban.db``, so callers open it independently and close it themselves.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

# SQLite's default host-parameter limit is 999; chunk IN-clauses well under it.
_IN_CHUNK = 500


def default_zeus_db_path() -> Path:
    """Location of the zeus token ledger, honouring ``HERMES_HOME``."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "zeus" / "zeus.db"


def connect(path: Optional[os.PathLike | str] = None) -> Optional[sqlite3.Connection]:
    """Open the zeus ledger read-only, or ``None`` if it doesn't exist.

    Missing file (zeus plugin never ran) or an unreadable DB both degrade to
    ``None`` so callers can treat "no ledger" and "no rows" uniformly.
    """
    p = Path(path) if path is not None else default_zeus_db_path()
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def aggregate_by_task(
    conn: Optional[sqlite3.Connection],
    task_ids: Iterable[str],
) -> dict[str, dict]:
    """Per-task token/cost totals for ``task_ids`` from the ledger.

    Returns ``{task_id: {"total_tokens", "prompt_tokens", "completion_tokens",
    "cost_usd", "last_ts"}}`` and only includes task ids that have at least one
    ledger row. A missing ``token_usage`` table or ``conn is None`` yields
    ``{}``. ``cost_usd`` is ``None`` when the ledger never priced the rows.
    """
    ids = [tid for tid in dict.fromkeys(task_ids) if tid]  # dedupe, drop empties
    if conn is None or not ids:
        return {}
    out: dict[str, dict] = {}
    for chunk in _chunks(ids, _IN_CHUNK):
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                "SELECT task_id, "
                "COALESCE(SUM(total_tokens), 0) AS total, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(completion_tokens), 0) AS completion, "
                "SUM(cost_usd) AS cost, "
                "MAX(ts) AS last_ts "
                f"FROM token_usage WHERE task_id IN ({placeholders}) "
                "GROUP BY task_id",
                tuple(chunk),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # ledger table absent (zeus plugin not installed)
        for r in rows:
            out[r["task_id"]] = {
                "total_tokens": int(r["total"] or 0),
                "prompt_tokens": int(r["prompt"] or 0),
                "completion_tokens": int(r["completion"] or 0),
                "cost_usd": float(r["cost"]) if r["cost"] is not None else None,
                "last_ts": float(r["last_ts"]) if r["last_ts"] is not None else None,
            }
    return out


def model_breakdown_by_task(
    conn: Optional[sqlite3.Connection],
    task_ids: Iterable[str],
) -> dict[str, list[dict]]:
    """Per-task, per-MODEL token split from the ledger (the real post-hoc model
    tag, not the intended one stamped at spawn).

    Returns ``{task_id: [{"model", "total_tokens", "prompt_tokens",
    "completion_tokens", "pct"}, ...]}`` sorted by ``total_tokens`` desc, with
    ``pct`` the model's share of that task's total rounded to 0.1%. A task that
    ran on a single model yields a one-element list (``pct`` ~100). Empty/absent
    model tags coalesce to ``"unknown"`` so honestly-unknown spend is visible
    rather than mislabelled. Only task ids with ledger rows appear. A missing
    ``token_usage`` table or ``conn is None`` yields ``{}``.
    """
    ids = [tid for tid in dict.fromkeys(task_ids) if tid]  # dedupe, drop empties
    if conn is None or not ids:
        return {}
    grouped: dict[str, list[dict]] = {}
    for chunk in _chunks(ids, _IN_CHUNK):
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                "SELECT task_id, "
                "COALESCE(NULLIF(TRIM(model), ''), 'unknown') AS model, "
                "COALESCE(SUM(total_tokens), 0) AS total, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(completion_tokens), 0) AS completion "
                f"FROM token_usage WHERE task_id IN ({placeholders}) "
                "GROUP BY task_id, model",
                tuple(chunk),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # ledger table absent (zeus plugin not installed)
        for r in rows:
            total = int(r["total"] or 0)
            if total <= 0:
                continue
            grouped.setdefault(r["task_id"], []).append({
                "model": r["model"],
                "total_tokens": total,
                "prompt_tokens": int(r["prompt"] or 0),
                "completion_tokens": int(r["completion"] or 0),
            })
    out: dict[str, list[dict]] = {}
    for tid, models in grouped.items():
        grand = sum(m["total_tokens"] for m in models)
        if grand <= 0:
            continue
        models.sort(key=lambda m: m["total_tokens"], reverse=True)
        for m in models:
            m["pct"] = round(m["total_tokens"] * 100.0 / grand, 1)
        out[tid] = models
    return out


def model_split(
    conn: Optional[sqlite3.Connection],
    task_id: str,
) -> Optional[dict]:
    """Full model split for one ``task_id`` plus the provider/effort/subscription
    facts observed across its runs.

    ``{"models": [...], "total_tokens": int, "providers": [...], "efforts":
    [...], "subscriptions": [...]}`` or ``None`` when the task burned no tokens
    (or the ledger is absent). The ``models`` list matches
    :func:`model_breakdown_by_task`. The fact lists are best-effort: on an older
    ledger that lacks the ``provider``/``effort``/``subscription`` columns they
    come back empty rather than raising.
    """
    by_task = model_breakdown_by_task(conn, [task_id])
    models = by_task.get(task_id)
    if not models:
        return None
    result: dict = {
        "models": models,
        "total_tokens": sum(m["total_tokens"] for m in models),
        "providers": [],
        "efforts": [],
        "subscriptions": [],
    }
    for col, key in (("provider", "providers"), ("effort", "efforts"),
                     ("subscription", "subscriptions")):
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {col} AS v FROM token_usage "
                "WHERE task_id = ? AND TRIM(COALESCE(" + col + ", '')) != ''",
                (task_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            continue  # column absent on this ledger — skip that fact
        result[key] = sorted(str(r["v"]).strip() for r in rows if r["v"])
    return result


def _cron_session_like(job_id: str) -> str:
    """LIKE pattern matching a cron job's run sessions.

    Cron runs tag every ``token_usage`` row with ``session_id`` =
    ``cron_<job_id>_<timestamp>`` (``cron/scheduler.py`` builds it as
    ``f"cron_{job_id}_{now:%Y%m%d_%H%M%S}"``). Cron job ids are fixed-length
    hex (``uuid4().hex[:12]``) so the literal underscores in the pattern — which
    SQLite ``LIKE`` treats as single-char wildcards — always sit over real
    underscores, and no id is a prefix of another; the match is exact for our
    data.
    """
    return f"cron_{job_id}_%"


def aggregate_by_cron(
    conn: Optional[sqlite3.Connection],
    job_ids: Iterable[str],
    since_ts: Optional[float] = None,
) -> dict[str, dict]:
    """Per-cron token/cost totals, keyed by the ``cron_<id>_*`` session prefix.

    Unlike :func:`aggregate_by_task`, cron spend is *not* grouped by ``task_id``
    (each firing gets a throwaway agent UUID there); the stable key is the run
    ``session_id`` prefix — see :func:`_cron_session_like`. ``since_ts`` (epoch
    seconds) optionally bounds the window to "spend over the last N days".

    Returns ``{job_id: {total_tokens, prompt_tokens, completion_tokens,
    cost_usd, run_count, last_ts}}`` — only ids with rows appear; ``run_count``
    counts distinct run sessions. ``conn is None`` or a missing ``token_usage``
    table yields ``{}``. Script-only crons (``no_agent``) never spend tokens, so
    they simply won't appear here.
    """
    ids = [jid for jid in dict.fromkeys(job_ids) if jid]  # dedupe, drop empties
    if conn is None or not ids:
        return {}
    out: dict[str, dict] = {}
    for jid in ids:
        params: list = [_cron_session_like(jid)]
        clause = "session_id LIKE ?"
        if since_ts is not None:
            clause += " AND ts >= ?"
            params.append(since_ts)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS total, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(completion_tokens), 0) AS completion, "
                "SUM(cost_usd) AS cost, "
                "COUNT(DISTINCT session_id) AS runs, "
                "MAX(ts) AS last_ts "
                f"FROM token_usage WHERE {clause}",
                tuple(params),
            ).fetchone()
        except sqlite3.OperationalError:
            return {}  # ledger table absent (zeus plugin not installed)
        if not row or not row["runs"]:
            continue  # no run sessions for this cron in-window
        out[jid] = {
            "total_tokens": int(row["total"] or 0),
            "prompt_tokens": int(row["prompt"] or 0),
            "completion_tokens": int(row["completion"] or 0),
            "cost_usd": float(row["cost"]) if row["cost"] is not None else None,
            "run_count": int(row["runs"] or 0),
            "last_ts": float(row["last_ts"]) if row["last_ts"] is not None else None,
        }
    return out


def per_run_token_totals(
    conn: Optional[sqlite3.Connection],
    job_id: str,
    since_ts: Optional[float] = None,
) -> list[int]:
    """Total tokens per cron run session, oldest run first.

    One entry per distinct ``cron_<job_id>_*`` session, summed across the turns
    within that run and ordered by when the run started. Feeds the
    token-spike baseline (compare the latest run against the median of the
    prior ones). ``conn is None`` / missing table -> ``[]``.
    """
    if conn is None or not job_id:
        return []
    params: list = [_cron_session_like(job_id)]
    clause = "session_id LIKE ?"
    if since_ts is not None:
        clause += " AND ts >= ?"
        params.append(since_ts)
    try:
        rows = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t, MIN(ts) AS started "
            f"FROM token_usage WHERE {clause} "
            "GROUP BY session_id ORDER BY started",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r["t"] or 0) for r in rows]


def build_children_map(links: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """``{parent_id: [child_id, ...]}`` from ``(parent_id, child_id)`` pairs."""
    m: dict[str, list[str]] = {}
    for parent, child in links:
        m.setdefault(parent, []).append(child)
    return m


def descendants(task_id: str, children_map: dict[str, list[str]]) -> set[str]:
    """All transitive children of ``task_id`` (cycle-safe, excludes itself)."""
    seen: set[str] = set()
    stack = list(children_map.get(task_id, ()))
    while stack:
        cur = stack.pop()
        if cur in seen or cur == task_id:
            continue
        seen.add(cur)
        stack.extend(children_map.get(cur, ()))
    return seen


def token_cost(
    task_id: str,
    per_task: dict[str, dict],
    descendant_ids: Iterable[str] = (),
) -> Optional[dict]:
    """Card-shaped token cost for ``task_id``: its own spend plus an epic rollup.

    ``per_task`` is the map from :func:`aggregate_by_task`; ``descendant_ids``
    are the task's transitive sub-tasks (empty for a leaf card). Returns::

        {
          "own":    {total_tokens, prompt_tokens, completion_tokens, cost_usd, last_ts},
          "rollup": {total_tokens, cost_usd, task_count},   # epics only
        }

    ``own`` is present only when this card itself burned tokens; ``rollup`` only
    when the card has descendants that did. Returns ``None`` when neither the
    card nor its sub-tasks have any ledger data, so callers can omit the badge.
    """
    own = per_task.get(task_id)
    desc = [d for d in dict.fromkeys(descendant_ids) if d != task_id and d in per_task]
    result: dict = {}
    if own:
        result["own"] = dict(own)
    if desc:
        roll_total = (own["total_tokens"] if own else 0) + sum(
            per_task[d]["total_tokens"] for d in desc
        )
        costs = [own["cost_usd"]] if own and own["cost_usd"] is not None else []
        costs += [per_task[d]["cost_usd"] for d in desc if per_task[d]["cost_usd"] is not None]
        result["rollup"] = {
            "total_tokens": roll_total,
            "cost_usd": round(sum(costs), 4) if costs else None,
            "task_count": len(desc) + (1 if own else 0),
        }
    return result or None


def humanize_tokens(n: int) -> str:
    """Compact token count for display (e.g. ``410000 -> '410K'``, ``1.5e6 -> '1.5M'``)."""
    if n >= 1_000_000:
        val = n / 1_000_000
        rounded = round(val)
        return f"{rounded}M" if abs(val - rounded) < 0.05 else f"{val:.1f}M"
    if n >= 1_000:
        val = n / 1_000
        rounded = round(val)
        return f"{rounded}K" if abs(val - rounded) < 0.05 else f"{val:.1f}K"
    return str(n)
