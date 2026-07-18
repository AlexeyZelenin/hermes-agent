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

**Restart survivability.** The weekly ``reset_at`` lives *only* in the
controller's ``pacing_state`` row. When the gateway restarts, the controller
re-derives each pocket's window from recent lease/probe activity — but a pocket
that is merely pacing (not cooling, no fresh limit-hits) has no such events, so
its window comes back un-computed: ``reset_at`` NULL, ``mode`` idle, spent 0,
even while the ledger shows the week's real burn. To bridge that gap this module
keeps a self-maintained recovery cache (``pacing_state_backup``): every snapshot
backs up each healthy pocket's window fields, and a snapshot that finds a blanked
row overlays the last-good backup (as long as its ``reset_at`` is still in the
future). Because the cache is refreshed continuously on read, it survives even a
hard kill (SIGKILL/SIGTERM) where an on-shutdown hook never runs. It only ever
writes its own backup table — the controller's ``pacing_state`` is never touched.

Everything degrades to empty when the ledger is absent (zeus plugin not
installed) or a table is missing: :func:`connect` returns ``None`` and
:func:`pacing_snapshot` returns an empty snapshot rather than raising. The DB is
opened and closed by the caller, exactly like :mod:`hermes_cli.zeus_tokens`.
"""

from __future__ import annotations

import math
import os
import sqlite3
from typing import Any, Optional

from hermes_cli import (
    pocket_accounting,
    subscription_limits,
    zeus_circuit_breaker,
    zeus_tokens,
)

# Fallback window length when the true window can't be pinned from the pacing
# row (Claude subscription limits reset weekly, so 7 days is the right default).
_DEFAULT_WINDOW_SECONDS = 7 * 24 * 3600

# The nested session window. Claude subscriptions meter a rolling 5h "session"
# limit *inside* the weekly one, and it is usually the binding wall — a burst
# trips it hours before the weekly budget is close. The controller only paces
# the weekly window, so :func:`_pocket` models this second, nested curve here.
# Matches ``agent.claude_subscriptions.LIMIT_WINDOW_SECONDS``.
_FIVE_HOUR_WINDOW_SECONDS = 5 * 3600

# The window fields this module snapshots into (and restores from) the recovery
# cache. Excludes the (board, subscription) key. ``updated_at`` is carried too so
# a restored pocket keeps its true staleness — the UI still flags the data as old.
_WINDOW_COLUMNS = (
    "window_label",
    "spent_percent",
    "target_percent",
    "elapsed_percent",
    "reset_at",
    "mode",
    "agent_limit",
    "burn_rate_per_min",
    "reason",
    "updated_at",
)

_BACKUP_DDL = """
CREATE TABLE IF NOT EXISTS pacing_state_backup (
    board TEXT NOT NULL,
    subscription TEXT NOT NULL,
    window_label TEXT,
    spent_percent REAL,
    target_percent REAL,
    elapsed_percent REAL,
    reset_at REAL,
    mode TEXT,
    agent_limit INTEGER,
    burn_rate_per_min REAL,
    reason TEXT,
    updated_at REAL,
    saved_at REAL NOT NULL,
    PRIMARY KEY (board, subscription)
)
"""


def _save_window_backup(
    conn: sqlite3.Connection, board: str, rows: list[sqlite3.Row], now: float
) -> None:
    """Cache each healthy pocket's window fields for restart recovery.

    Only rows whose ``reset_at`` is set are backed up, so a controller row that
    was blanked by a restart never overwrites the last-good backup we need to
    restore *from*. Best-effort: a read-only DB or write contention degrades to a
    no-op — the cache is an optimization, never a correctness dependency.
    """
    healthy = [r for r in rows if r["reset_at"] is not None]
    if not healthy:
        return
    try:
        conn.execute(_BACKUP_DDL)
        conn.executemany(
            "INSERT INTO pacing_state_backup "
            "(board, subscription, window_label, spent_percent, target_percent, "
            "elapsed_percent, reset_at, mode, agent_limit, burn_rate_per_min, "
            "reason, updated_at, saved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(board, subscription) DO UPDATE SET "
            "window_label=excluded.window_label, spent_percent=excluded.spent_percent, "
            "target_percent=excluded.target_percent, "
            "elapsed_percent=excluded.elapsed_percent, reset_at=excluded.reset_at, "
            "mode=excluded.mode, agent_limit=excluded.agent_limit, "
            "burn_rate_per_min=excluded.burn_rate_per_min, reason=excluded.reason, "
            "updated_at=excluded.updated_at, saved_at=excluded.saved_at",
            [
                (
                    board,
                    r["subscription"],
                    r["window_label"],
                    r["spent_percent"],
                    r["target_percent"],
                    r["elapsed_percent"],
                    r["reset_at"],
                    r["mode"],
                    r["agent_limit"],
                    r["burn_rate_per_min"],
                    r["reason"],
                    r["updated_at"],
                    now,
                )
                for r in healthy
            ],
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _restore_window(
    conn: sqlite3.Connection, board: str, row: sqlite3.Row, now: float
) -> "sqlite3.Row | dict[str, Any]":
    """Overlay the last-good window backup onto a row the controller blanked.

    A restart leaves a pacing row with a NULL ``reset_at`` (window un-computed).
    When that happens, pull the cached window fields and overlay them so the
    pocket shows its last-known pacing state instead of a phantom idle/0%. The
    backup is only used while its ``reset_at`` is still in the future — a reset
    that already elapsed means the window genuinely rolled over during downtime,
    so we leave the pocket idle for the controller to re-establish. Rows that
    already carry a ``reset_at`` pass through untouched.
    """
    if row["reset_at"] is not None:
        return row
    try:
        backup = conn.execute(
            "SELECT window_label, spent_percent, target_percent, elapsed_percent, "
            "reset_at, mode, agent_limit, burn_rate_per_min, reason, updated_at "
            "FROM pacing_state_backup WHERE board = ? AND subscription = ?",
            (board, row["subscription"]),
        ).fetchone()
    except sqlite3.OperationalError:
        return row
    if backup is None or backup["reset_at"] is None or backup["reset_at"] <= now:
        return row
    merged = dict(row)
    for col in _WINDOW_COLUMNS:
        merged[col] = backup[col]
    return merged


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
    """``{name: {display_name, enabled, reserved, cooling_until, last_limited_at}}``.

    Enriches each pocket with its human label, reserved flag, and cool-down
    state from ``claude_subscriptions``. Pacing observes a reserved pocket for
    measurement only — it is never leased to workers (task t_83c4b740). Empty
    when that table is absent; ``reserved`` degrades to ``False`` on a pool DB
    predating the column.
    """
    try:
        rows = conn.execute(
            "SELECT name, display_name, enabled, cooling_until, last_limited_at "
            "FROM claude_subscriptions"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    reserved_by_name = _reserved_flags(conn)
    dirs_by_name = _config_dirs(conn)
    return {
        r["name"]: {
            "display_name": r["display_name"] or "",
            "enabled": bool(r["enabled"]),
            "reserved": reserved_by_name.get(r["name"], False),
            "config_dir": dirs_by_name.get(r["name"], ("", "claude"))[0],
            "provider": dirs_by_name.get(r["name"], ("", "claude"))[1],
            "cooling_until": (
                float(r["cooling_until"]) if r["cooling_until"] is not None else None
            ),
            "last_limited_at": (
                float(r["last_limited_at"]) if r["last_limited_at"] is not None else None
            ),
        }
        for r in rows
    }


def _reserved_flags(conn: sqlite3.Connection) -> dict[str, bool]:
    """``{name: reserved}`` — tolerant of a pool DB predating the column."""
    try:
        rows = conn.execute(
            "SELECT name, reserved FROM claude_subscriptions"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["name"]: bool(r["reserved"]) for r in rows}


def _config_dirs(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``{name: (config_dir, provider)}`` — the login dir each pocket's sessions
    write under, so interactive spend can be attributed to it (task t_5580f23b).
    Tolerant of a pool DB predating the ``provider`` column (defaults to claude)."""
    try:
        rows = conn.execute(
            "SELECT name, config_dir, provider FROM claude_subscriptions"
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = conn.execute(
                "SELECT name, config_dir FROM claude_subscriptions"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["name"]: (r["config_dir"] or "", "claude") for r in rows}
    return {
        r["name"]: (r["config_dir"] or "", (r["provider"] or "claude"))
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


def _interactive_tokens(
    meta: dict, window_start: Optional[float], until: float, exclude: frozenset[str]
) -> Optional[int]:
    """Interactive (non-worker) tokens this pocket burned in a window, or ``None``.

    Best-effort filesystem read of the pocket's session logs (task t_5580f23b);
    any error degrades to ``None`` so the live pacing view never breaks on it.
    """
    config_dir = meta.get("config_dir") or ""
    if not config_dir or window_start is None:
        return None
    try:
        result = pocket_accounting.interactive_window_tokens(
            config_dir, meta.get("provider") or "claude",
            window_start=window_start, until_ts=until, exclude_sessions=exclude,
        )
    except Exception:
        return None
    return result["total_tokens"]


def _pocket(
    row: "sqlite3.Row | dict[str, Any]",
    subs: dict[str, dict],
    conn: sqlite3.Connection,
    now: float,
    exclude_sessions: frozenset[str],
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
    # Interactive (operator/supervisor) spend attributed to this pocket by its
    # login dir — the runtime count the ledger alone misses (task t_5580f23b).
    # Additive/observational here: reported alongside worker ``window_tokens`` so
    # the panel/`/subs` show real burn; it does not (yet) feed the breaker verdict.
    interactive_week = _interactive_tokens(meta, window_start, now, exclude_sessions)
    interactive_5h = _interactive_tokens(meta, five_start, now, exclude_sessions)
    return {
        "subscription": row["subscription"],
        "display_name": meta.get("display_name") or row["subscription"],
        "enabled": meta.get("enabled"),
        "reserved": meta.get("reserved", False),
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
        "config_dir": meta.get("config_dir") or "",
        "provider": meta.get("provider") or "claude",
        # Interactive (non-worker) tokens attributed to this pocket by its login
        # dir, over the weekly and 5h windows. ``None`` when the dir can't be read.
        "interactive_week_tokens": interactive_week,
        "interactive_5h_tokens": interactive_5h,
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
    _save_window_backup(conn, board, rows, now)
    exclude = frozenset(pocket_accounting.attributed_session_ids(conn))
    pockets = [
        _pocket(_restore_window(conn, board, r, now), subs, conn, now, exclude)
        for r in rows
    ]
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
