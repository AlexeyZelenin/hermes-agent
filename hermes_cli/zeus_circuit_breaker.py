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

    Returns a dict carrying the state, the live/projected figures behind it,
    and the ``recommended_agent_limit`` a consumer may cap to (``0`` open,
    ``1`` half-open, ``None`` otherwise). Combine several windows'
    verdicts with :func:`aggregate`.
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
    projected = (
        live_spent / frac if live_spent is not None and frac and frac > 0 else None
    )

    state, reason = _classify(live_spent, frac, projected, cooling)
    return {
        "state": state,
        "tripped": state == "open",
        "throttling": state == "half_open",
        "burning_down": state == "burndown",
        "recommended_agent_limit": _LIMIT_BY_STATE[state],
        # True when spent-% was scaled from the live ledger; False when it fell
        # back to the controller's stale reading (no budget could be derived).
        "live_derived": budget is not None,
        "live_spent_percent": round(live_spent, 1) if live_spent is not None else None,
        "projected_spent_percent": round(projected, 1) if projected is not None else None,
        "pace_delta": round(pace_delta, 1) if pace_delta is not None else None,
        "implied_budget_tokens": int(budget) if budget is not None else None,
        "elapsed_fraction": round(frac, 4) if frac is not None else None,
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
    """
    known = [v for v in verdicts if v and v.get("state") != "unknown"]
    winner = min(known, key=lambda v: _STATE_RANK[v["state"]]) if known else None
    if winner is None:
        state, reason, limit = "unknown", "insufficient pacing data", None
    else:
        state = winner["state"]
        reason = winner.get("reason", "")
        limit = _LIMIT_BY_STATE[state]
    return {
        "state": state,
        "tripped": state == "open",
        "throttling": state == "half_open",
        "burning_down": state == "burndown",
        "recommended_agent_limit": limit,
        "reason": reason,
        "windows": list(verdicts),
    }
