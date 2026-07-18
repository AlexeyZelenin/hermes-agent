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

import math
import os
import sqlite3
from typing import Optional

from hermes_cli import subscription_limits, zeus_circuit_breaker, zeus_tokens

# Fallback window length when the true window can't be pinned from the pacing
# row (Claude subscription limits reset weekly, so 7 days is the right default).
_DEFAULT_WINDOW_SECONDS = 7 * 24 * 3600

# The nested session window. Claude subscriptions meter a rolling 5h "session"
# limit *inside* the weekly one, and it is usually the binding wall — a burst
# trips it hours before the weekly budget is close. The controller only paces
# the weekly window, so :func:`_pocket` models this second, nested curve here.
# Matches ``agent.claude_subscriptions.LIMIT_WINDOW_SECONDS``.
_FIVE_HOUR_WINDOW_SECONDS = 5 * 3600


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


def _five_hour_window(anchor: Optional[float], now: float) -> tuple[float, float]:
    """``(start, reset)`` of the 5h session window containing ``now``.

    The window is anchored on the *client's* reported reset (``anchor`` —
    typically ``cooling_until``, the reset time the provider handed back on the
    last limit-hit), which is a point on the true 5h grid. We slide by whole 5h
    steps from that anchor to the block around ``now``, so the nested window
    stays phase-aligned with the provider's real reset rather than an arbitrary
    clock offset. With no anchor, fall back to the epoch-aligned 5h boundary
    (the same grid ``agent.claude_subscriptions.next_window_boundary`` uses).
    """
    w = _FIVE_HOUR_WINDOW_SECONDS
    if anchor is None:
        reset = (int(now) // w + 1) * w
        return float(reset - w), float(reset)
    steps = math.floor((now - anchor) / w) + 1
    reset = anchor + steps * w
    return reset - w, reset


def _five_hour_budget(
    weekly_budget: Optional[float],
    weekly_start: Optional[float],
    reset_at: Optional[float],
) -> Optional[float]:
    """Session-window token budget implied by the weekly one, or ``None``.

    The controller calibrates only the weekly pocket, so there is no direct
    session budget. Under the operator's even-rate doctrine ("deplete slowly,
    evenly") a 5h session's fair share is the weekly budget scaled by its slice
    of the week — ``weekly_budget * 5h / week_length``. Capping each session to
    that share is what stops a single block from front-loading the week and
    tripping the session wall. ``None`` when the weekly budget or length can't
    be pinned (the session verdict then degrades to ``unknown`` — fail open).

    Once the pocket has hit a limit and revealed its true session cap, the
    empirical measurement (task ``t_e38bbe56``,
    :func:`hermes_cli.subscription_limits.measured_session_limit`) supersedes this
    derived share in :func:`_session_breaker`; this remains the cold-start
    fallback. The curve around either budget is identical.
    """
    if weekly_budget is None or weekly_start is None or reset_at is None:
        return None
    weekly_len = reset_at - weekly_start
    if weekly_len <= 0:
        return None
    return weekly_budget * (_FIVE_HOUR_WINDOW_SECONDS / weekly_len)


def _window_tokens(
    conn: sqlite3.Connection,
    subscription: str,
    since_ts: Optional[float],
    until_ts: Optional[float] = None,
) -> Optional[dict]:
    """``{total_tokens, turns, last_ts}`` burned by ``subscription`` in-window.

    Sums ``token_usage`` rows tagged with this subscription since
    ``since_ts`` (the window start). ``until_ts`` optionally caps the upper
    bound (used by the v2 circuit-breaker to read spend *as of* the controller's
    last snapshot). ``None`` when the ledger table is absent.
    """
    clause = "subscription = ?"
    params: list = [subscription]
    if since_ts is not None:
        clause += " AND ts >= ?"
        params.append(since_ts)
    if until_ts is not None:
        clause += " AND ts <= ?"
        params.append(until_ts)
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


def _session_breaker(
    conn: sqlite3.Connection,
    subscription: str,
    *,
    cooling_until: Optional[float],
    weekly_budget: Optional[int],
    weekly_start: Optional[float],
    reset_at: Optional[float],
    now: float,
    cooling: bool,
) -> tuple[dict, float, float, Optional[dict]]:
    """Nested 5h-session verdict + its window bounds and in-window tokens.

    The controller only paces the weekly pocket, so this evaluates the second,
    nested curve: the same live ledger read over the current 5h session window
    (anchored on the client reset), judged against the session budget derived
    from the weekly one under the even-rate doctrine. Returns
    ``(breaker_5h, start, reset, tokens)``.
    """
    start, reset = _five_hour_window(cooling_until, now)
    tokens = _window_tokens(conn, subscription, start)
    # Prefer the empirically-measured session cap (task t_e38bbe56) — a real
    # limit-hit measurement gives the breaker a precise budget; fall back to the
    # weekly-share estimate only until enough measurements exist.
    budget = subscription_limits.measured_session_limit(conn, subscription)
    if budget is None:
        budget = _five_hour_budget(weekly_budget, weekly_start, reset_at)
    breaker = zeus_circuit_breaker.evaluate(
        spent_percent=None,
        live_tokens=tokens["total_tokens"] if tokens is not None else None,
        snapshot_tokens=None,
        window_start=start,
        reset_at=reset,
        now=now,
        cooling=cooling,
        budget_tokens=budget,
    )
    return breaker, start, reset, tokens


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
    cooling = cooling_until is not None and cooling_until > now
    live = _window_tokens(conn, row["subscription"], window_start)
    # v2 real-time circuit-breaker: re-derive spend from the live ledger, using
    # tokens burned up to ``updated_at`` as the controller's calibration point.
    snapshot = _window_tokens(conn, row["subscription"], window_start, until_ts=updated_at)
    breaker = zeus_circuit_breaker.evaluate(
        spent_percent=spent,
        live_tokens=live["total_tokens"] if live is not None else None,
        snapshot_tokens=snapshot["total_tokens"] if snapshot is not None else None,
        window_start=window_start,
        reset_at=reset_at,
        now=now,
        cooling=cooling,
    )
    # Fold the nested session verdict into the weekly one: open (either wall)
    # halts, a session burndown in the block's tail lifts a weekly throttle to
    # drain the remainder, else the tighter throttle wins (see aggregate()).
    breaker_5h, five_start, five_reset, five_live = _session_breaker(
        conn,
        row["subscription"],
        cooling_until=cooling_until,
        weekly_budget=breaker["implied_budget_tokens"],
        weekly_start=window_start,
        reset_at=reset_at,
        now=now,
        cooling=cooling,
    )
    effective = zeus_circuit_breaker.aggregate([breaker, breaker_5h])
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
        "cooling": cooling,
        "last_limited_at": meta.get("last_limited_at"),
        "window_start": window_start,
        "window_tokens": live,
        "circuit_breaker": breaker,
        "circuit_breaker_5h": breaker_5h,
        "five_hour_window": {
            "start": five_start,
            "reset": five_reset,
            "seconds_to_reset": max(0.0, five_reset - now),
            "tokens": five_live,
        },
        # Board-effective verdict: weekly ∧ session folded (see aggregate()).
        # ``recommended_agent_limit`` here is the cap the board should obey.
        "effective_breaker": effective,
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
    burn rate, time to reset), cool-down state, the tokens it burned in the
    current window, and a ``circuit_breaker`` verdict (pacing v2 — the real-time
    spend cutoff, see :mod:`hermes_cli.zeus_circuit_breaker`). Alongside it a
    ``circuit_breaker_5h`` verdict over the nested 5h-session window and an
    ``effective_breaker`` that folds the two (the cap the board should obey).
    ``conn is None``
    or a missing ``pacing_state`` table yields
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
