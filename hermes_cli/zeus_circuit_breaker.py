"""Pacing v2 — a real-time circuit-breaker over live spend.

The v1 pacing controller (see :mod:`hermes_cli.zeus_pacing`) upserts a
``pacing_state`` row only *every few minutes*. Between those cycles a burst of
agents can blow through the window's budget before the stale ``spent_percent``
catches up. This module is the fast safety cutoff layered on top: given the
controller's last snapshot plus the *live* append-only ``token_usage`` ledger,
it re-derives how much of the budget is spent **right now**, projects the
end-of-window spend, and returns a circuit-breaker verdict.

The trick that keeps it real-time without hardcoding a token budget: the
controller's ``spent_percent`` is a calibration of "tokens ↔ % of budget" as of
its ``updated_at``. Divide the tokens burned up to that instant by that percent
and we recover the *implied budget*; feed today's live token total through the
same scale and ``spent_percent`` keeps advancing between controller cycles.

Everything is pure — no sqlite, no clock — so it is trivially testable and the
caller (:mod:`hermes_cli.zeus_pacing`) supplies the ledger totals. It **fails
open**: on missing or contradictory telemetry the verdict is ``closed`` (never
halt the whole board because a number is absent), the same degrade-to-nothing
philosophy as the rest of the zeus read layer.

Breaker states (classic vocabulary):

``closed``
    On or near pace. No breaker-imposed cap (``recommended_agent_limit`` None).
``half_open``
    Spending ahead of pace but not projected to exhaust the budget — throttle
    to a trickle (``recommended_agent_limit`` 1).
``open``
    Tripped: projected to exhaust the window budget before it resets, or the
    provider already rate-limited the pocket (cooling). Halt new spawns
    (``recommended_agent_limit`` 0).
``burndown``
    Late in the window and *under*-utilizing — on this pace the window would
    reset with budget left unspent (wasted: subscription allowances don't roll
    over). Release the cap and drain the remainder (``recommended_agent_limit``
    None). This is the operator doctrine "deplete slowly, but finish the window
    empty": in :func:`aggregate` a window in burndown *lifts* a throttle another
    window imposed, so a reserved/throttled pocket still gets burned down.
``unknown``
    Not enough data to judge — treated as ``closed`` (fail open).
"""

from __future__ import annotations

from typing import Optional

# --- Tunables --------------------------------------------------------------
# The trip signal is the *linear end-of-window projection*: live spend divided
# by the elapsed fraction, i.e. "at this rate, what % of budget by reset?".
# Trip (open) once that projects to overshoot the budget by this margin...
TRIP_PROJECTED_PERCENT = 120.0
# ...and throttle (half_open) in the band from merely-on-track-to-exhaust up to
# the trip line. Below WARN there is comfortable headroom -> closed.
WARN_PROJECTED_PERCENT = 100.0
# Always trip, regardless of pace, once this much of the budget is already gone
# — the wall is imminent whatever the projection says.
HARD_SPENT_PERCENT = 95.0
# Don't lean on the projection in the opening slice of the window: a modest
# burst in the first minutes divides by a near-zero elapsed fraction and
# projects to absurd figures (2% at 0.5% elapsed -> 400%) that reflect noise,
# not risk. Inside this slice only the HARD_SPENT near-exhaustion trip applies.
MIN_ELAPSED_FRACTION = 0.02

# Idle override (task t_4ee09bd0). The lifetime-average projection self-locks a
# pocket that burned hard once (day-one onboarding burst) and has since gone
# idle: ``spent/elapsed`` keeps projecting an overshoot even though the burn rate
# is now ~0. When a recent rate is supplied and the spend it would add over the
# REMAINING window is under this % of budget, the pocket counts as idle — its
# projection holds at current spend (no growth), so it can't trip on a stale
# extrapolation. 10% means "even if this trickle ran the whole rest of the
# window, it would add <10% of the budget" — genuinely idle, not just slow.
IDLE_GROWTH_PERCENT = 10.0

# Burndown band. Only in the tail of the window (past this elapsed fraction)...
BURNDOWN_ELAPSED_FRACTION = 0.9
# ...and only when the linear projection lands this far under a full window do
# we call it under-utilization worth draining. At 0.9 elapsed a projection of
# <90% means live spend is <81% -> real budget would be left on the table.
BURNDOWN_MAX_PROJECTED_PERCENT = 90.0

# Breaker-imposed agent cap per state; None means "no cap, defer to dispatcher".
_LIMIT_BY_STATE = {
    "open": 0,
    "half_open": 1,
    "burndown": None,
    "closed": None,
    "unknown": None,
}

# Aggregation precedence (lower folds first): a hard stop dominates a burndown,
# which overrides a throttle, which overrides an on-pace window. See aggregate().
_STATE_RANK = {"open": 0, "burndown": 1, "half_open": 2, "closed": 3, "unknown": 4}


def implied_budget_tokens(
    spent_percent: Optional[float],
    snapshot_tokens: Optional[int],
) -> Optional[float]:
    """Window budget in tokens implied by the controller's calibration.

    ``snapshot_tokens`` is what the ledger shows was burned up to the
    controller's ``updated_at``; ``spent_percent`` is the % of budget that
    represented. ``budget = tokens / (percent/100)``. ``None`` when either
    input is missing or non-positive (can't invert a zero).
    """
    if spent_percent is None or spent_percent <= 0:
        return None
    if snapshot_tokens is None or snapshot_tokens <= 0:
        return None
    return snapshot_tokens / (spent_percent / 100.0)


def live_spent_percent(
    live_tokens: Optional[int],
    budget_tokens: Optional[float],
    fallback_percent: Optional[float],
) -> Optional[float]:
    """Real-time spent-% of budget, or the controller's stale % as a fallback.

    With an implied budget we scale today's live token total through it; when
    the budget can't be derived (no calibration) we hand back the controller's
    last ``spent_percent`` so the breaker still has *something* to judge on.
    """
    if budget_tokens is not None and budget_tokens > 0 and live_tokens is not None:
        return live_tokens / budget_tokens * 100.0
    return fallback_percent


def elapsed_fraction(
    window_start: Optional[float],
    reset_at: Optional[float],
    now: float,
) -> Optional[float]:
    """Fraction of the current window elapsed at ``now``, clamped to ``[0, 1]``.

    ``None`` when the window can't be pinned (missing bounds or non-positive
    length).
    """
    if window_start is None or reset_at is None:
        return None
    length = reset_at - window_start
    if length <= 0:
        return None
    return min(1.0, max(0.0, (now - window_start) / length))


def _classify(
    live_spent: Optional[float],
    elapsed_frac: Optional[float],
    projected: Optional[float],
    cooling: bool,
) -> tuple[str, str]:
    """Map the live figures onto a ``(state, reason)`` pair.

    Precedence: a provider cooldown is a definitional trip; then near-exhaustion
    of the budget (whatever the pace); then the projection bands — but the
    projection is trusted only once past the opening slice of the window;
    finally, in the tail of the window, an under-utilizing pace flips to
    ``burndown`` so the remainder gets drained before it resets and is lost.
    """
    if cooling:
        return "open", "provider rate-limited (cooling)"
    if live_spent is None:
        return "unknown", "insufficient pacing data"
    if live_spent >= HARD_SPENT_PERCENT:
        return "open", f"spent {live_spent:.0f}% of budget"
    if (
        projected is not None
        and elapsed_frac is not None
        and elapsed_frac >= MIN_ELAPSED_FRACTION
    ):
        if projected >= TRIP_PROJECTED_PERCENT:
            return "open", f"projected {projected:.0f}% of budget by reset"
        if projected >= WARN_PROJECTED_PERCENT:
            return "half_open", f"projected {projected:.0f}% of budget by reset"
    if (
        projected is not None
        and elapsed_frac is not None
        and elapsed_frac >= BURNDOWN_ELAPSED_FRACTION
        and projected < BURNDOWN_MAX_PROJECTED_PERCENT
    ):
        return "burndown", f"projected {projected:.0f}% of budget by reset — burning down"
    return "closed", "on pace"


def evaluate(
    *,
    spent_percent: Optional[float],
    live_tokens: Optional[int],
    snapshot_tokens: Optional[int],
    window_start: Optional[float],
    reset_at: Optional[float],
    now: float,
    cooling: bool = False,
    budget_tokens: Optional[float] = None,
    recent_rate_per_sec: Optional[float] = None,
) -> dict:
    """Circuit-breaker verdict for one subscription pocket, over one window.

    ``snapshot_tokens`` = ledger tokens burned up to the controller's
    ``updated_at`` (the calibration point); ``live_tokens`` = ledger tokens
    burned so far this window (up to ``now``). ``spent_percent`` is the
    controller's last reading, used to derive the implied budget and as the
    fallback when no budget can be derived. ``cooling`` reflects a provider
    rate-limit already in force (a definitional trip).

    ``budget_tokens`` lets a caller supply the window's token budget directly
    instead of inverting it from ``spent_percent``/``snapshot_tokens``. The
    nested 5h-session window uses this (:mod:`hermes_cli.zeus_pacing` derives
    its budget from the weekly one), since the controller only calibrates the
    weekly pocket.

    ``recent_rate_per_sec`` is the pocket's *recent* token burn rate
    (tokens/sec over the last ~2h, supplied by the live wiring in
    :mod:`hermes_cli.zeus_pacing`). It serves two purposes:

    1. **Idle override.** The lifetime-average projection (``live_spent /
       elapsed_frac``) self-locks a pocket that burned hard once (a day-one
       onboarding burst) and has since gone idle — ``spent/elapsed`` keeps
       projecting an overshoot even though the burn rate is now ~0 (task
       t_4ee09bd0). When a recent rate is supplied AND the spend it would add
       over the REMAINING window is under ``IDLE_GROWTH_PERCENT`` of budget,
       the pocket counts as idle: its projection holds at current spend (no
       growth → no projection trip). Active pockets keep the lifetime average.
    2. **Graceful admission.** When the pocket is actively burning faster than
       the rate that would land it at 100% of budget by reset, the
       ``sustainable_rate_ratio`` lets the dispatcher cap its in-flight count
       instead of slamming it to zero.

    When ``None`` (no recent rate) the legacy lifetime-average projection is
    used, so callers and tests that don't supply a rate behave exactly as before.

    Returns a dict carrying the state, the live/projected figures behind it,
    and the ``recommended_agent_limit`` a consumer may cap to (``0`` open,
    ``1`` half-open, ``None`` otherwise). Two dispatch signals ride alongside:

    ``hard_block``
        ``True`` only for genuine walls — a provider cooldown or near-exhaustion
        (>= ``HARD_SPENT_PERCENT``). These are the cases where admitting ANY
        task would blow a real limit, so the dispatcher must skip the pocket
        entirely. Projection-only overshoot is NOT a hard block: it is paced
        gracefully via ``sustainable_rate_ratio`` instead.
    ``sustainable_rate_ratio``
        The fraction of the *current* burn rate that would land the pocket at
        exactly 100% of budget by reset (``target_rate / recent_rate``).
        ``None`` when there is no recent rate, no remaining budget, or the
        pocket is on/under pace (nothing to throttle). ``< 1.0`` means the
        pocket is overshooting and the dispatcher should cap its concurrency to
        bring the rate down smoothly — *graceful admission* instead of slamming
        it to zero. Combine several windows' verdicts with :func:`aggregate`.
    ``idle``
        ``True`` when the idle override held the projection at current spend
        (diagnostic — the pocket's recent burn is negligible).
    """
    budget = (
        budget_tokens
        if budget_tokens is not None and budget_tokens > 0
        else implied_budget_tokens(spent_percent, snapshot_tokens)
    )
    live_spent = live_spent_percent(live_tokens, budget, spent_percent)
    frac = elapsed_fraction(window_start, reset_at, now)

    pace_delta = (
        live_spent - frac * 100.0
        if live_spent is not None and frac is not None
        else None
    )
    seconds_left = (
        reset_at - now if reset_at is not None and reset_at > now else None
    )
    # ---- end-of-window projection ------------------------------------------------
    # The lifetime average (``live_spent / elapsed_frac``) is the right pace
    # estimate for a pocket that's ACTIVELY burning — but it self-locks a pocket
    # that burned hard once (a day-one onboarding burst) and has since gone IDLE:
    # the stale average projects an overshoot even though nothing is burning now
    # (task t_4ee09bd0 — the operator's kimi case: 16% at 11% elapsed, burn rate
    # 0.0). So when a recent rate is supplied we apply an IDLE OVERRIDE: if the
    # recent rate, sustained over the remaining window, would add negligible
    # spend, the pocket is effectively idle and the projection holds at current
    # spend (no growth → no projection trip). Active pockets keep the lifetime
    # average (their real pace); the recent rate separately drives the graceful-
    # admission ratio below. ``None`` rate → legacy lifetime average (back-compat).
    idle = False
    if (
        recent_rate_per_sec is not None
        and budget is not None
        and budget > 0
        and seconds_left is not None
        and seconds_left > 0
    ):
        growth_pct = recent_rate_per_sec * seconds_left / budget * 100.0
        if growth_pct < IDLE_GROWTH_PERCENT:
            idle = True
    if recent_rate_per_sec is not None and idle:
        # Idle: hold at current spend — a stale onboarding burst can't trip.
        projected = live_spent
    else:
        projected = (
            live_spent / frac if live_spent is not None and frac and frac > 0 else None
        )

    # ---- graceful-admission ratio ------------------------------------------------
    # What fraction of the current burn rate lands the pocket at exactly 100%
    # of budget by reset. Only emitted when the pocket is genuinely overshooting
    # (ratio < 1.0); on/under pace -> None (no cap). Requires a recent rate
    # (graceful admission from the legacy average would re-introduce the very
    # lifetime-average poisoning the recent rate exists to fix) and remaining
    # budget. The dispatcher multiplies this by its known in-flight count.
    sustainable_rate_ratio: Optional[float] = None
    if (
        recent_rate_per_sec is not None
        and recent_rate_per_sec > 0
        and budget is not None
        and budget > 0
        and seconds_left is not None
        and seconds_left > 0
    ):
        spent_frac = (live_spent or 0.0) / 100.0
        remaining_budget_tokens = budget * max(0.0, 1.0 - spent_frac)
        if remaining_budget_tokens > 0:
            target_rate = remaining_budget_tokens / seconds_left
            ratio = target_rate / recent_rate_per_sec
            if 0.0 <= ratio < 1.0:
                sustainable_rate_ratio = ratio

    state, reason = _classify(live_spent, frac, projected, cooling)
    # Hard block: a real wall where admitting any task risks blowing the limit.
    # Cooling is provider-enforced; near-exhaustion means the wall is imminent
    # whatever the pace. Projection-only overshoot is NOT here — that paces
    # gracefully via sustainable_rate_ratio so the pocket trickles rather than
    # slams to zero.
    hard_block = bool(
        cooling or (live_spent is not None and live_spent >= HARD_SPENT_PERCENT)
    )
    return {
        "state": state,
        "tripped": state == "open",
        "throttling": state == "half_open",
        "burning_down": state == "burndown",
        "recommended_agent_limit": _LIMIT_BY_STATE[state],
        "hard_block": hard_block,
        "sustainable_rate_ratio": (
            round(sustainable_rate_ratio, 4)
            if sustainable_rate_ratio is not None
            else None
        ),
        "idle": idle,
        # True when spent-% was scaled from the live ledger; False when it fell
        # back to the controller's stale reading (no budget could be derived).
        "live_derived": budget is not None,
        "live_spent_percent": round(live_spent, 1) if live_spent is not None else None,
        "projected_spent_percent": round(projected, 1) if projected is not None else None,
        "pace_delta": round(pace_delta, 1) if pace_delta is not None else None,
        "implied_budget_tokens": int(budget) if budget is not None else None,
        "elapsed_fraction": round(frac, 4) if frac is not None else None,
        "seconds_to_reset": seconds_left,
        "recent_rate_per_sec": recent_rate_per_sec,
        "reason": reason,
    }


def aggregate(verdicts: list[dict]) -> dict:
    """Fold several nested-window verdicts into the one the board obeys.

    A pocket is paced by more than one window at once — the weekly allowance
    and the 5h session allowance nested inside it — and each yields its own
    :func:`evaluate` verdict. The effective cap is *not* a plain ``min`` in
    every case; the precedence encodes the operator doctrine:

    #. **open wins.** A hard wall in *any* window (projected overshoot, near
       exhaustion, or a provider cooldown) halts new spawns — this is the
       ``min(weekly, session)`` of the spec, with open's limit ``0`` as the
       floor. You can never burn into a real limit.
    #. **then burndown wins.** With no open window, a window in ``burndown``
       *lifts* a throttle another window imposed and releases the cap: the tail
       of a window must be drained even for a reserved/throttled pocket
       ("finish the window empty; personal's reserve does not survive
       burndown"). This is the one place the effective cap is deliberately
       looser than a raw ``min``.
    #. **else the throttle min.** ``half_open`` (limit 1) beats ``closed``
       (no cap).

    Empty input, or all-``unknown``, folds to an ``unknown`` verdict (fail
    open — never halt the board because telemetry is missing). The winning
    window's ``state``/``reason`` carry through; ``windows`` keeps the
    components for the dashboard.

    Graceful-admission signals fold alongside the state:

    ``hard_block``
        ``True`` if ANY window hard-blocks (cooldown or near-exhaustion). The
        dispatcher skips the pocket entirely when this is set.
    ``sustainable_rate_ratio``
        The MIN ratio across all windows that emit one — the tightest rate
        discipline wins, since either window's budget is binding. ``None`` when
        no window has a rate (no graceful cap).
    ``recent_rate_per_sec``
        The max recent rate across windows (the actual burn we'd be pacing).
    """
    known = [v for v in verdicts if v and v.get("state") != "unknown"]
    winner = min(known, key=lambda v: _STATE_RANK[v["state"]]) if known else None
    if winner is None:
        state, reason, limit = "unknown", "insufficient pacing data", None
    else:
        state = winner["state"]
        reason = winner.get("reason", "")
        limit = _LIMIT_BY_STATE[state]
    # Fold graceful-admission signals across every window.
    hard_block = any(bool(v.get("hard_block")) for v in verdicts if v)
    ratios = [
        v["sustainable_rate_ratio"]
        for v in verdicts
        if v and v.get("sustainable_rate_ratio") is not None
    ]
    rates = [
        v["recent_rate_per_sec"]
        for v in verdicts
        if v and v.get("recent_rate_per_sec") is not None
    ]
    # Burn-down overrides the throttle signals: a window in its tail must be
    # drained, so a pocket burning down is not rate-capped (None) and not hard-
    # blocked by projection (only a genuine cooldown/exhaustion in *another*
    # window still binds via hard_block above).
    sustainable: Optional[float] = min(ratios) if ratios else None
    if state == "burndown":
        sustainable = None
    return {
        "state": state,
        "tripped": state == "open",
        "throttling": state == "half_open",
        "burning_down": state == "burndown",
        "recommended_agent_limit": limit,
        "hard_block": hard_block,
        "sustainable_rate_ratio": sustainable,
        "recent_rate_per_sec": max(rates) if rates else None,
        "reason": reason,
        "windows": list(verdicts),
    }


# --- graceful admission --------------------------------------------------------

# Floor for the rate-based admission cap: even a pocket well over its
# sustainable rate keeps this many workers in flight, so the pocket trickles
# (paces down smoothly) instead of slamming to a full stop and self-locking.
# 1 == "let at least one task run at a time"; raise for a wider trickle.
ADMISSION_LIMIT_FLOOR = 1


def admission_agent_limit(
    *,
    sustainable_rate_ratio: Optional[float],
    base_in_flight: int,
    floor: int = ADMISSION_LIMIT_FLOOR,
) -> Optional[int]:
    """Graceful per-pocket concurrency cap from a sustainable-rate ratio.

    The dispatcher's replacement for the binary "tripped -> block all" rule.
    Given the pocket's ``sustainable_rate_ratio`` (from :func:`evaluate` /
    :func:`aggregate` — what fraction of the current burn rate lands the pocket
    at 100% of budget by reset) and the pocket's current ``base_in_flight``
    worker count, return the capped in-flight count that brings the burn rate
    down to the sustainable one.

    Concretely: ``max(floor, round(base * ratio))``. A pocket at half its
    sustainable rate (ratio 0.5) with 4 in flight caps to 2; one at 0.34 caps
    to 1; the floor guarantees a trickle rather than a deadlock.

    Returns ``None`` (no cap — dispatch normally) when:

    - ``sustainable_rate_ratio`` is ``None`` (no recent rate / on-or-under pace /
      burning down — nothing to throttle), or
    - ``base_in_flight`` is not a positive int (no workers to scale), or
    - the computed cap is ``>= base_in_flight`` (the ratio says the pocket can
      afford its current concurrency — no throttle needed).

    So a ``None`` return is unambiguously "no throttle": callers gate on
    ``cap is not None and cap < base``.
    """
    if sustainable_rate_ratio is None:
        return None
    if sustainable_rate_ratio >= 1.0:
        # On or under pace — the breaker wouldn't have emitted a ratio < 1.0,
        # but guard against an aggregate edge so this never over-throttles.
        return None
    if base_in_flight <= 0:
        return None
    if floor < 0:
        floor = 0
    cap = round(base_in_flight * sustainable_rate_ratio)
    if cap < floor:
        cap = floor
    if cap >= base_in_flight:
        return None
    return cap
