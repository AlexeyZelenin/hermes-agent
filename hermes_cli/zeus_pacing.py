"""Read-only view over the zeus pacing state (``~/.hermes/zeus/zeus.db``).

The per-board, per-subscription *pacing state* is written by the external
*zeus* pacing controller (the same plugin that owns the token ledger — see
:mod:`hermes_cli.zeus_tokens`). Every few minutes it upserts a ``pacing_state``
row per subscription pocket: how much of the window's budget is spent, the
target it's pacing toward, how much of the window has elapsed, the current
mode (``throttle`` when spending too fast, ``burndown`` when it should use the
rest before reset, ``idle`` otherwise), the agent-concurrency limit it derived,
and when the window resets.

This module gives the Zeus dashboard a read-only "statusline" view of that
state — per pocket: spent% vs target% vs elapsed%, mode, agent limit, burn
rate, time to reset — plus the actual ``token_usage`` burned in the current
window. It answers the operator's terminal-statusline question inside the UI:
*how much have I used, how long until reset, am I on pace?*

Everything degrades to empty when the ledger is absent (zeus plugin not
installed) or a table is missing: :func:`connect` returns ``None`` and
:func:`pacing_snapshot` returns an empty snapshot rather than raising. The DB is
opened and closed by the caller, exactly like :mod:`hermes_cli.zeus_tokens`.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from hermes_cli import zeus_tokens

# Fallback window length when the true window can't be pinned from the pacing
# row (Claude subscription limits reset weekly, so 7 days is the right default).
_DEFAULT_WINDOW_SECONDS = 7 * 24 * 3600


def connect(path: Optional[os.PathLike | str] = None) -> Optional[sqlite3.Connection]:
    """Open the zeus ledger read-only, or ``None`` if it doesn't exist.

    Thin re-export of :func:`hermes_cli.zeus_tokens.connect` — pacing state and
    the token ledger live in the *same* ``zeus.db``, so they share one opener.
    """
    return zeus_tokens.connect(path)


def _window_start(
    reset_at: Optional[float],
    elapsed_percent: Optional[float],
    updated_at: Optional[float],
) -> Optional[float]:
    """Epoch at which the current pacing window began, or ``None``.

    The pacing row records, as of ``updated_at``, both the window's end
    (``reset_at``) and the fraction of it elapsed (``elapsed_percent``). With
    ``length = (reset_at - updated_at) / (1 - elapsed_frac)`` we recover the
    window length without hardcoding the cadence, then ``reset_at - length`` is
    the start. Falls back to a 7-day lookback when the inputs can't pin it down
    (missing fields, or a degenerate elapsed fraction outside ``(0, 1)``).
    """
    if reset_at is None:
        return None
    if elapsed_percent is None or updated_at is None:
        return reset_at - _DEFAULT_WINDOW_SECONDS
    frac = elapsed_percent / 100.0
    if not 0.0 < frac < 1.0:
        return reset_at - _DEFAULT_WINDOW_SECONDS
    length = (reset_at - updated_at) / (1.0 - frac)
    if length <= 0:
        return reset_at - _DEFAULT_WINDOW_SECONDS
    return reset_at - length


def _window_tokens(
    conn: sqlite3.Connection,
    subscription: str,
    since_ts: Optional[float],
) -> Optional[dict]:
    """``{total_tokens, turns, last_ts}`` burned by ``subscription`` in-window.

    Sums ``token_usage`` rows tagged with this subscription since
    ``since_ts`` (the window start). ``None`` when the ledger table is absent.
    """
    clause = "subscription = ?"
    params: list = [subscription]
    if since_ts is not None:
        clause += " AND ts >= ?"
        params.append(since_ts)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t, COUNT(*) AS n, MAX(ts) AS last "
            f"FROM token_usage WHERE {clause}",
            tuple(params),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return {
        "total_tokens": int(row["t"] or 0),
        "turns": int(row["n"] or 0),
        "last_ts": float(row["last"]) if row["last"] is not None else None,
    }


def _subscription_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{name: {display_name, enabled, cooling_until, last_limited_at}}``.

    Enriches each pocket with its human label and cool-down state from
    ``claude_subscriptions``. Empty when that table is absent.
    """
    try:
        rows = conn.execute(
            "SELECT name, display_name, enabled, cooling_until, last_limited_at "
            "FROM claude_subscriptions"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        r["name"]: {
            "display_name": r["display_name"] or "",
            "enabled": bool(r["enabled"]),
            "cooling_until": (
                float(r["cooling_until"]) if r["cooling_until"] is not None else None
            ),
            "last_limited_at": (
                float(r["last_limited_at"]) if r["last_limited_at"] is not None else None
            ),
        }
        for r in rows
    }


def _pocket(
    row: sqlite3.Row,
    subs: dict[str, dict],
    conn: sqlite3.Connection,
    now: float,
) -> dict:
    """Shape one ``pacing_state`` row into a dashboard pocket dict."""
    spent = row["spent_percent"]
    target = row["target_percent"]
    reset_at = row["reset_at"]
    updated_at = row["updated_at"]
    meta = subs.get(row["subscription"], {})
    cooling_until = meta.get("cooling_until")
    pace_delta = spent - target if spent is not None and target is not None else None
    window_start = _window_start(reset_at, row["elapsed_percent"], updated_at)
    return {
        "subscription": row["subscription"],
        "display_name": meta.get("display_name") or row["subscription"],
        "enabled": meta.get("enabled"),
        "window_label": row["window_label"],
        "spent_percent": spent,
        "target_percent": target,
        "elapsed_percent": row["elapsed_percent"],
        # >0 means spent has run ahead of the pacing target (limit risk); <=0
        # means there's still slack. ``on_track`` is the sign flipped to a bool.
        "pace_delta": round(pace_delta, 1) if pace_delta is not None else None,
        "on_track": pace_delta <= 0 if pace_delta is not None else None,
        "mode": row["mode"],
        "agent_limit": row["agent_limit"],
        "burn_rate_per_min": row["burn_rate_per_min"],
        "reset_at": reset_at,
        "seconds_to_reset": max(0.0, reset_at - now) if reset_at is not None else None,
        "reason": row["reason"],
        "updated_at": updated_at,
        "staleness_seconds": (
            max(0.0, now - updated_at) if updated_at is not None else None
        ),
        "cooling_until": cooling_until,
        "cooling": cooling_until is not None and cooling_until > now,
        "last_limited_at": meta.get("last_limited_at"),
        "window_start": window_start,
        "window_tokens": _window_tokens(conn, row["subscription"], window_start),
    }


def pacing_snapshot(
    conn: Optional[sqlite3.Connection],
    board: str,
    *,
    now: float,
) -> dict:
    """Per-pocket pacing snapshot for ``board`` — the dashboard's status view.

    Returns ``{"board", "now", "pockets": [...], "window_total_tokens"}``. Each
    pocket carries its pacing state (spent/target/elapsed %, mode, agent limit,
    burn rate, time to reset), cool-down state, and the tokens it burned in the
    current window. ``conn is None`` or a missing ``pacing_state`` table yields
    an empty ``pockets`` list rather than raising, so the panel degrades to a
    "no pacing data" state instead of a 500.
    """
    empty = {"board": board, "now": now, "pockets": [], "window_total_tokens": 0}
    if conn is None:
        return empty
    subs = _subscription_meta(conn)
    try:
        rows = conn.execute(
            "SELECT * FROM pacing_state WHERE board = ? ORDER BY subscription",
            (board,),
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    pockets = [_pocket(r, subs, conn, now) for r in rows]
    total = sum(
        p["window_tokens"]["total_tokens"]
        for p in pockets
        if p["window_tokens"] is not None
    )
    return {
        "board": board,
        "now": now,
        "pockets": pockets,
        "window_total_tokens": total,
    }
