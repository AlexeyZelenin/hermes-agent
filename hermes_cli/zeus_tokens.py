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
