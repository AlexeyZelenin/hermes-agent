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
import time
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

# --- graceful admission / EMA-weighted projection ----------------------------
# The breaker's projection must reflect RECENT burn, not the lifetime average
# since window start (a day-one onboarding burst would otherwise poison the
# whole week — task t_4ee09bd0). ``_recent_burn_rate`` reconstructs an EMA of
# the token burn rate over this lookback window from the append-only ledger.
_RECENT_RATE_LOOKBACK_SEC = 2 * 3600  # 2h of samples feed the EMA
_RECENT_RATE_TAU_SEC = 1800           # 30-min time constant: recent samples dominate

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


def _recent_burn_rate(
    conn: sqlite3.Connection,
    subscription: str,
    *,
    now: float,
    since_ts: Optional[float] = None,
    lookback_sec: float = _RECENT_RATE_LOOKBACK_SEC,
    tau_sec: float = _RECENT_RATE_TAU_SEC,
) -> Optional[float]:
    """Exponentially-weighted recent token burn rate (tokens/sec), or ``None``.

    The projection the breaker makes must reflect the pocket's *recent* burn,
    not the lifetime average since the window started (a day-one onboarding
    burst that's long since gone idle would otherwise project to an overshoot
    for the entire week — the self-lock false-positive). This reconstructs an
    EMA of the burn rate over the last ``lookback_sec`` from the append-only
    ledger, weighting each row by ``exp(-(now - ts) / tau)`` so recent spend
    dominates.

    Returns ``0.0`` when the ledger is readable but has NO rows in the lookback
    — that is a DEFINITIVE idle signal (the pocket genuinely hasn't burned
    recently), distinct from ``None`` (ledger unreadable / table absent). ``0.0``
    is what lets the breaker's idle override fire for a pocket with no recent
    spend, instead of falling back to the stale lifetime average. Without this
    distinction a day-one onboarding burst that's since gone quiet would keep
    self-locking on the lifetime projection forever (task t_4ee09bd0).

    ``since_ts`` clamps the lower bound to the window start when supplied: we
    only ever pace spend inside the window we're judging (a weekly window
    ignores last week's tail; a 5h window ignores earlier sessions).
    """
    start = now - lookback_sec
    if since_ts is not None and since_ts > start:
        start = since_ts
    if start >= now:
        # Window started in the future (clock skew / fresh window): no spend
        # can exist yet -> definitively idle, not unknown.
        return 0.0
    clause = "subscription = ? AND ts >= ? AND ts <= ?"
    params: list = [subscription, start, now]
    try:
        rows = conn.execute(
            f"SELECT ts, total_tokens FROM token_usage WHERE {clause} "
            "ORDER BY ts ASC",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        # Ledger readable, no recent spend -> idle (0.0), NOT unknown.
        return 0.0
    alpha = 1.0 / tau_sec if tau_sec > 0 else 1.0
    # Time-weighted token sum / (lookback span), where each row's tokens are
    # weighted by exp(-alpha * age). Equivalent to integrating the EMA impulse
    # train and normalizing by the lookback window.
    weighted_tokens = 0.0
    for r in rows:
        age = max(0.0, now - float(r["ts"]))
        weighted_tokens += float(r["total_tokens"] or 0) * math.exp(-alpha * age)
    span = now - start
    if span <= 0:
        return None
    return weighted_tokens / span


def _pacing_config_providers(conn: sqlite3.Connection) -> dict[str, str]:
    """``{subscription: provider}`` from the controller's ``pacing_config`` table.

    Non-Claude pockets (zai, kimi-coding, …) have no row in
    ``claude_subscriptions`` — they are virtual pockets the zeus controller
    creates from each vendor's usage API. Their provider lives in
    ``pacing_config.provider`` instead. This reads that mapping so the read
    path can label them correctly rather than defaulting every pocket to
    ``claude``. Empty on a DB predating the table or the column.
    """
    try:
        rows = conn.execute(
            "SELECT subscription, provider FROM pacing_config "
            "WHERE provider IS NOT NULL AND provider != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["subscription"]: r["provider"] for r in rows if r["provider"]}


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
    providers = _pacing_config_providers(conn)
    return {
        r["name"]: {
            "display_name": r["display_name"] or "",
            "enabled": bool(r["enabled"]),
            "reserved": reserved_by_name.get(r["name"], False),
            "config_dir": dirs_by_name.get(r["name"], ("", "claude"))[0],
            # Non-Claude pockets (glm/kimi/…) aren't in claude_subscriptions;
            # their provider comes from pacing_config. When both speak, the
            # pool table wins (it is the authoritative registry).
            "provider": (
                dirs_by_name.get(r["name"], ("", ""))[1]
                or providers.get(r["name"], "claude")
            ),
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
    recent_rate_per_sec: Optional[float] = None,
) -> tuple[dict, float, float, Optional[dict]]:
    """Nested 5h-session verdict + its window bounds and in-window tokens.

    The controller only paces the weekly pocket, so this evaluates the second,
    nested curve: the same live ledger read over the current 5h session window
    (anchored on the client reset), judged against the session budget derived
    from the weekly one under the even-rate doctrine. Returns
    ``(breaker_5h, start, reset, tokens)``.

    ``recent_rate_per_sec`` (5h-window-restricted) drives the EMA-weighted
    projection and graceful-admission ratio for this nested curve.
    """
    start, reset = _five_hour_window(cooling_until, now)
    tokens = _window_tokens(conn, subscription, start)
    # Prefer the empirically-measured session cap (task t_e38bbe56) — a real
    # limit-hit measurement gives the breaker a precise budget; fall back to the
    # weekly-share estimate only until enough measurements exist.
    budget = subscription_limits.measured_session_limit(conn, subscription)
    if budget is None:
        budget = _five_hour_budget(weekly_budget, weekly_start, reset_at)
    # Restrict the recent-rate EMA to spend inside THIS 5h session window.
    rate_5h = recent_rate_per_sec
    if rate_5h is None:
        rate_5h = _recent_burn_rate(conn, subscription, now=now, since_ts=start)
    breaker = zeus_circuit_breaker.evaluate(
        spent_percent=None,
        live_tokens=tokens["total_tokens"] if tokens is not None else None,
        snapshot_tokens=None,
        window_start=start,
        reset_at=reset,
        now=now,
        cooling=cooling,
        budget_tokens=budget,
        recent_rate_per_sec=rate_5h,
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


def _row_spent_tokens(row: "sqlite3.Row | dict[str, Any]") -> Optional[int]:
    """Vendor-reported tokens spent in this window, or ``None``.

    The zeus controller writes ``spent_tokens`` for non-Claude pockets (zai,
    kimi-coding) taken directly from each vendor's usage API. Claude pockets
    leave it NULL because their spend is reconstructed from the token ledger.
    Tolerant of a pacing_state predating the column.
    """
    val = row.get("spent_tokens") if isinstance(row, dict) else (
        row["spent_tokens"] if "spent_tokens" in row.keys() else None
    )
    return int(val) if val is not None else None


def _pocket(
    row: "sqlite3.Row | dict[str, Any]",
    subs: dict[str, dict],
    conn: sqlite3.Connection,
    now: float,
    exclude_sessions: frozenset[str],
    *,
    provider_override: Optional[str] = None,
    session_row: Optional["sqlite3.Row | dict[str, Any]"] = None,
) -> dict:
    """Shape one ``pacing_state`` row into a dashboard pocket dict.

    ``provider_override`` lets the caller pin a non-Claude pocket's provider
    (zai / kimi-coding / …) when the pool registry has no entry for it. The
    default is the registry's provider, falling back to ``claude``.

    ``session_row`` is the *other* pacing_state row for split-window pockets
    (non-Claude vendors whose usage API reports a 5h session window and a
    weekly window as two rows). When supplied, the nested 5h verdict is built
    from that row's vendor-reported spent% / reset_at / spent_tokens instead
    of being derived from the token ledger — the vendor's number is the
    authoritative one for those pockets, and the ledger has no rows for them.
    """
    spent = row["spent_percent"]
    target = row["target_percent"]
    reset_at = row["reset_at"]
    updated_at = row["updated_at"]
    meta = subs.get(row["subscription"], {})
    cooling_until = meta.get("cooling_until")
    pace_delta = spent - target if spent is not None and target is not None else None
    window_start = _window_start(reset_at, row["elapsed_percent"], updated_at)
    cooling = cooling_until is not None and cooling_until > now
    provider = provider_override or meta.get("provider") or "claude"
    # Vendor-reported spend for this window (non-Claude). When present, the
    # token_usage ledger read below is informational only — the vendor's
    # number already counts every token burned against this account, including
    # interactive use the local ledger never sees.
    vendor_spent = _row_spent_tokens(row)
    live = _window_tokens(conn, row["subscription"], window_start)
    # If the ledger has nothing for this pocket (non-Claude), fall back to the
    # vendor-reported ``spent_tokens`` so the dashboard shows a real number
    # instead of 0.
    if (
        live is not None
        and live["total_tokens"] == 0
        and vendor_spent not in (None, 0)
    ):
        live = {**live, "total_tokens": vendor_spent}
    # v2 real-time circuit-breaker: re-derive spend from the live ledger, using
    # as-of ``updated_at`` as the controller's calibration point. Non-Claude
    # pockets skip the breaker entirely — their vendor reports the authoritative
    # spent%, and the circuit-breaker's ledger-derived budget does not apply.
    snapshot = _window_tokens(conn, row["subscription"], window_start, until_ts=updated_at)
    # EMA-weighted recent burn rate over the last ~2h, restricted to spend
    # inside this (weekly) window. Drives the projection that replaces the
    # lifetime average, and the graceful-admission ratio. None when the ledger
    # has no recent rows for this pocket — the breaker then holds at current
    # spend rather than extrapolating a stale average.
    recent_rate = _recent_burn_rate(
        conn, row["subscription"], now=now, since_ts=window_start
    )
    breaker = zeus_circuit_breaker.evaluate(
        spent_percent=spent,
        live_tokens=live["total_tokens"] if live is not None else None,
        snapshot_tokens=snapshot["total_tokens"] if snapshot is not None else None,
        window_start=window_start,
        reset_at=reset_at,
        now=now,
        cooling=cooling,
        recent_rate_per_sec=recent_rate,
    )
    # Fold the nested session verdict into the weekly one: open (either wall)
    # halts, a session burndown in the block's tail lifts a weekly throttle to
    # drain the remainder, else the tighter throttle wins (see aggregate()).
    # Note: the session breaker computes its OWN recent rate restricted to the
    # 5h window (passing the weekly rate would judge the session curve on the
    # wrong window's burn).
    if session_row is not None:
        breaker_5h, five_start, five_reset, five_live = _vendor_session_window(
            session_row, now=now
        )
    else:
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
        "provider": provider,
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


def _vendor_session_window(
    row: "sqlite3.Row | dict[str, Any]", *, now: float
) -> tuple[dict, float, float, Optional[dict]]:
    """Build the nested 5h verdict from a vendor-reported session row.

    Non-Claude vendors (z.ai, Moonshot) publish a real 5h-session spent%
    and reset directly in their usage API — the controller writes them as a
    separate ``Current session`` pacing_state row. That number already counts
    every token burned against the account, so the verdict is a plain
    circuit-breaker evaluation against the spent% alone (no ledger, no
    derived budget — the vendor is authoritative). Returns
    ``(breaker_5h, start, reset, tokens_dict)`` mirroring ``_session_breaker``.
    """
    reset_at = row["reset_at"]
    spent = row["spent_percent"]
    start = _window_start(reset_at, row["elapsed_percent"], row["updated_at"])
    breaker = zeus_circuit_breaker.evaluate(
        spent_percent=spent,
        live_tokens=None,
        snapshot_tokens=None,
        window_start=start,
        reset_at=reset_at,
        now=now,
        cooling=False,
    )
    spent_tok = _row_spent_tokens(row)
    tokens = {"total_tokens": spent_tok or 0, "turns": 0, "last_ts": None}
    return breaker, start or 0.0, reset_at or 0.0, tokens


def _is_session_label(label: str) -> bool:
    """Heuristic: does this window_label describe the 5h session window?"""
    if not label:
        return False
    low = label.strip().lower()
    return "session" in low or "5h" in low or "5 h" in low or "session" in low


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

    Non-Claude pockets (zai, kimi-coding, …) are emitted as a single merged
    pocket even when the controller writes them as two rows (Current session +
    Current week) — the session row feeds the nested 5h window, the weekly row
    is the primary, and the vendor-reported ``spent_percent`` / ``spent_tokens``
    are used directly. The token ledger is only consulted for Claude pockets.
    """
    empty = {"board": board, "now": now, "pockets": [], "window_total_tokens": 0}
    if conn is None:
        return empty
    subs = _subscription_meta(conn)
    providers = _pacing_config_providers(conn)
    try:
        rows = conn.execute(
            "SELECT * FROM pacing_state WHERE board = ? ORDER BY subscription",
            (board,),
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    _save_window_backup(conn, board, rows, now)
    exclude = frozenset(pocket_accounting.attributed_session_ids(conn))
    # Group rows by subscription. Claude pockets → one row (weekly); the nested
    # 5h verdict is derived from the ledger. Non-Claude pockets → up to two
    # rows (Current session + Current week) which we merge into one pocket so
    # the dashboard renders one card with both windows filled from the vendor.
    by_sub: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_sub.setdefault(r["subscription"], []).append(r)
    pockets: list[dict] = []
    for name, group in by_sub.items():
        provider_hint = providers.get(name)
        if len(group) == 1:
            pockets.append(_pocket(
                _restore_window(conn, board, group[0], now),
                subs, conn, now, exclude,
                provider_override=provider_hint,
            ))
            continue
        # Split-window pocket: pick the weekly row as the primary and the
        # session row as the nested window. If neither label matches the
        # heuristic, the first row is primary and the second is the session —
        # the controller always emits the weekly row first.
        weekly = next((r for r in group if not _is_session_label(r["window_label"])), group[0])
        session = next((r for r in group if _is_session_label(r["window_label"])), group[-1])
        pockets.append(_pocket(
            _restore_window(conn, board, weekly, now),
            subs, conn, now, exclude,
            provider_override=provider_hint,
            session_row=_restore_window(conn, board, session, now),
        ))
    # Stable order: Claude pockets keep their source order; non-Claude pockets
    # (merged from dual rows) are sorted by name after the Claude ones so the
    # panel groups providers readably. The dashboard's own fallback sort still
    # runs on top of this.
    pockets.sort(key=lambda p: (p["provider"] == "claude", p["subscription"]))
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


def pocket_admission(
    conn: Optional[sqlite3.Connection],
    board: str,
    *,
    now: float,
) -> dict[str, dict]:
    """``{pocket_name: admission_verdict}`` for every paced, enabled pocket.

    The dispatch-gating view of pacing — what the kanban dispatcher matches a
    ready task's assignee against. Replaces the binary "tripped -> block all"
    rule with **graceful admission**: a pocket whose window is overshooting the
    sustainable rate is *rate-capped* (admit fewer concurrent tasks), not
    slammed to zero. Only genuine walls hard-block.

    Each verdict dict carries:

    ``hard_block`` (bool)
        ``True`` only for real walls — a provider cooldown or near-exhaustion
        (>= ``HARD_SPENT_PERCENT``). The dispatcher skips the pocket entirely
        while this is set. Projection-only overshoot is NOT a hard block.
    ``sustainable_rate_ratio`` (float | None)
        The tightest rate ratio across the pocket's windows (weekly ∧ 5h
        session). ``< 1.0`` means the pocket is burning faster than the rate
        that would land it at 100% of budget by reset, and the dispatcher
        should cap its in-flight count to ``round(base * ratio)`` (floored to
        1) — *graceful admission*. ``None`` when the pocket is on/under pace,
        has no recent burn rate, or is burning down (no cap).
    ``recent_rate_per_sec`` (float | None)
        The actual recent burn rate (max across windows) — diagnostic.
    ``reason`` (str)
        The winning window's reason (for the ``pacing_throttled`` /
        ``pacing_admission`` event log).
    ``state`` (str)
        Effective breaker state (open / half_open / burndown / closed).

    Tracking-only pockets (``pacing_config.enabled = 0``) are excluded — they
    are observed but never dispatch-gated, so a projection with no real
    allowance can't self-lock the pocket (or the very card that would fix it).
    Degrades to ``{}`` (fail open) on any error or a missing ledger.
    """
    try:
        snapshot = pacing_snapshot(conn, board, now=now)
    except Exception:
        return {}
    by_name: dict[str, list[dict]] = {}
    for pocket in snapshot.get("pockets", []):
        # Tracking-only pocket: operator set pacing_config.enabled=0, so we
        # observe its spend but never dispatch-gate on it. Skipping here (not
        # just in actuation) is what makes enabled=0 actually stop the throttle
        # — otherwise a projection with no real allowance self-locks the pocket
        # (and even the very card that would fix it). enabled is None for rows
        # with no config: treat as enabled (default gating preserved).
        if pocket.get("enabled") is False:
            continue
        verdict = pocket.get("effective_breaker")
        name = pocket.get("subscription")
        if verdict and name:
            by_name.setdefault(name, []).append(verdict)
    admission: dict[str, dict] = {}
    for name, verdicts in by_name.items():
        folded = zeus_circuit_breaker.aggregate(verdicts)
        # Exclude pockets with no pacing telemetry at all (all-unknown) — there
        # is nothing to gate on, and failing open keeps the board moving.
        if folded.get("state") == "unknown" and not folded.get("hard_block"):
            continue
        admission[name] = {
            "hard_block": bool(folded.get("hard_block")),
            "sustainable_rate_ratio": folded.get("sustainable_rate_ratio"),
            "recent_rate_per_sec": folded.get("recent_rate_per_sec"),
            "reason": folded.get("reason") or "pocket throttled",
            "state": folded.get("state", "unknown"),
        }
    return admission


def throttled_pockets(
    conn: Optional[sqlite3.Connection],
    board: str,
    *,
    now: float,
) -> dict[str, str]:
    """``{pocket_name: reason}`` for every HARD-BLOCKED pocket right now.

    The hard-block subset of :func:`pocket_admission` — only the pockets the
    dispatcher must skip entirely because admitting ANY task would risk blowing
    a real limit (a provider cooldown, or near-exhaustion of the budget).
    Projection-only overshoot is deliberately NOT here: that is paced
    *gracefully* via the sustainable-rate ratio instead of slammed to zero
    (task t_4ee09bd0 — graceful admission replaces the binary throttle).

    Kept as a thin view over :func:`pocket_admission` for callers that only
    need the skip-all signal (dashboards, diagnostics). The dispatcher itself
    uses :func:`pocket_admission` directly so it can apply the graceful cap.
    Degrades to ``{}`` (fail open) on any error or a missing ledger.
    """
    admission = pocket_admission(conn, board, now=now)
    return {
        name: v["reason"]
        for name, v in admission.items()
        if v.get("hard_block")
    }


def pocket_admission_for_board(
    board: str, *, now: Optional[float] = None
) -> dict[str, dict]:
    """Open the zeus ledger and return :func:`pocket_admission` for ``board``.

    Convenience wrapper for callers (the kanban dispatcher) that don't hold a
    zeus connection. Opens and closes the read-only ledger like the dashboard's
    pacing route. Returns ``{}`` when the ledger is absent (zeus plugin not
    installed) or on any error — fail open.
    """
    if now is None:
        now = time.time()
    conn = connect()
    if conn is None:
        return {}
    try:
        return pocket_admission(conn, board, now=now)
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def throttled_pockets_for_board(
    board: str, *, now: Optional[float] = None
) -> dict[str, str]:
    """Open the zeus ledger and return :func:`throttled_pockets` for ``board``.

    Convenience wrapper for callers that don't hold a zeus connection. Returns
    ``{}`` when the ledger is absent (zeus plugin not installed) or on any
    error — fail open.
    """
    if now is None:
        now = time.time()
    conn = connect()
    if conn is None:
        return {}
    try:
        return throttled_pockets(conn, board, now=now)
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
