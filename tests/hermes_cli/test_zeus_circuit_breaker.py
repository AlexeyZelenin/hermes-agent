"""Tests for hermes_cli.zeus_circuit_breaker — pacing v2 real-time spend cutoff.

Pure logic, no I/O: every case feeds token totals + window bounds straight into
the evaluator. Nothing here touches a live ledger or the operator's real
subscriptions.
"""

from __future__ import annotations

import pytest

from hermes_cli import zeus_circuit_breaker as cb

NOW = 1_784_400_000.0
WEEK = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Budget derivation from the controller calibration
# ---------------------------------------------------------------------------

def test_implied_budget_from_spent_percent():
    # 30% of the budget was 300 tokens -> budget is 1000 tokens.
    assert cb.implied_budget_tokens(30.0, 300) == pytest.approx(1000.0)


def test_implied_budget_none_on_zero_or_missing():
    assert cb.implied_budget_tokens(0.0, 300) is None
    assert cb.implied_budget_tokens(None, 300) is None
    assert cb.implied_budget_tokens(30.0, 0) is None
    assert cb.implied_budget_tokens(30.0, None) is None


def test_live_spent_scales_through_budget():
    # 700 live tokens against a 1000-token budget -> 70%.
    assert cb.live_spent_percent(700, 1000.0, 30.0) == pytest.approx(70.0)


def test_live_spent_falls_back_to_controller_percent():
    # No derivable budget -> hand back the stale controller reading.
    assert cb.live_spent_percent(700, None, 42.0) == 42.0
    assert cb.live_spent_percent(700, 0.0, 42.0) == 42.0


# ---------------------------------------------------------------------------
# Elapsed fraction
# ---------------------------------------------------------------------------

def test_elapsed_fraction_midwindow():
    frac = cb.elapsed_fraction(NOW - WEEK / 2, NOW + WEEK / 2, NOW)
    assert frac == pytest.approx(0.5)


def test_elapsed_fraction_clamped_and_degenerate():
    # Past reset clamps to 1.0; before start clamps to 0.0.
    assert cb.elapsed_fraction(NOW - WEEK, NOW - 1, NOW) == 1.0
    assert cb.elapsed_fraction(NOW + 100, NOW + WEEK, NOW) == 0.0
    # Missing / non-positive length -> None.
    assert cb.elapsed_fraction(None, NOW, NOW) is None
    assert cb.elapsed_fraction(NOW, NOW, NOW) is None


# ---------------------------------------------------------------------------
# evaluate() — the state machine
#
# Default fixture: budget = snapshot 500 / 50% = 1000 tokens; window is one week
# with NOW at the halfway mark (elapsed 50%). Vary ``live_tokens`` to move the
# live spent-% (live_tokens / 1000 * 100) and thus the projection (spent/0.5).
# ---------------------------------------------------------------------------

def _eval(**kw):
    base = dict(
        spent_percent=50.0,
        live_tokens=500,
        snapshot_tokens=500,
        window_start=NOW - WEEK / 2,
        reset_at=NOW + WEEK / 2,
        now=NOW,
        cooling=False,
    )
    base.update(kw)
    return cb.evaluate(**base)


def test_closed_on_pace():
    # 50% spent at 50% elapsed -> projected 100%... which is exactly the WARN
    # line. Stay just under it: 48% spent -> projected 96% -> closed.
    v = _eval(live_tokens=480)
    assert v["state"] == "closed"
    assert v["tripped"] is False
    assert v["recommended_agent_limit"] is None
    assert v["live_spent_percent"] == pytest.approx(48.0)
    assert v["live_derived"] is True
    assert v["reason"] == "on pace"


def test_half_open_when_projected_to_just_exhaust():
    # 55% spent at 50% elapsed -> projected 110%: past WARN (100), below TRIP
    # (120) -> throttle to a trickle.
    v = _eval(live_tokens=550)
    assert v["state"] == "half_open"
    assert v["throttling"] is True
    assert v["recommended_agent_limit"] == 1
    assert v["projected_spent_percent"] == pytest.approx(110.0)


def test_open_when_projected_to_overshoot():
    # 65% spent at 50% elapsed -> projected 130% (>= TRIP 120) -> trip.
    v = _eval(live_tokens=650)
    assert v["state"] == "open"
    assert v["tripped"] is True
    assert v["recommended_agent_limit"] == 0
    assert v["projected_spent_percent"] == pytest.approx(130.0)
    assert "projected" in v["reason"]


def test_open_on_near_exhaustion_regardless_of_pace():
    # 96% already spent — the wall is imminent even though it's late (95%
    # elapsed) and the projection (96/0.95 ~ 101%) alone would only warn.
    v = cb.evaluate(
        spent_percent=96.0,
        live_tokens=960,  # budget = snapshot 960 / 96% = 1000 -> live 96%
        snapshot_tokens=960,
        window_start=NOW - WEEK * 0.95,
        reset_at=NOW + WEEK * 0.05,
        now=NOW,
    )
    assert v["state"] == "open"
    assert "spent 96% of budget" in v["reason"]


def test_no_projection_trip_in_opening_slice():
    # 10% spent at 1% elapsed projects to 1000%, but we're inside MIN_ELAPSED
    # (2%): the projection is noise, not risk, so it stays closed. The figure is
    # still reported for the dashboard.
    v = cb.evaluate(
        spent_percent=10.0,
        live_tokens=100,  # budget 100/0.10 = 1000 -> 10%
        snapshot_tokens=100,
        window_start=NOW - WEEK * 0.01,
        reset_at=NOW + WEEK * 0.99,
        now=NOW,
    )
    assert v["state"] == "closed"
    assert v["projected_spent_percent"] is not None


def test_near_exhaustion_trips_even_in_opening_slice():
    # The HARD_SPENT trip is not gated by elapsed: 97% spent in the first 1% of
    # the window still trips (someone dumped nearly the whole budget at once).
    v = cb.evaluate(
        spent_percent=97.0,
        live_tokens=970,
        snapshot_tokens=970,
        window_start=NOW - WEEK * 0.01,
        reset_at=NOW + WEEK * 0.99,
        now=NOW,
    )
    assert v["state"] == "open"


def test_cooling_forces_open():
    v = _eval(live_tokens=100, cooling=True)  # otherwise comfortably closed
    assert v["state"] == "open"
    assert v["recommended_agent_limit"] == 0
    assert "cooling" in v["reason"]


def test_unknown_when_no_spend_signal_at_all():
    v = cb.evaluate(
        spent_percent=None,
        live_tokens=500,
        snapshot_tokens=None,
        window_start=None,
        reset_at=None,
        now=NOW,
    )
    assert v["state"] == "unknown"
    assert v["tripped"] is False
    assert v["recommended_agent_limit"] is None
    assert v["live_derived"] is False


def test_falls_back_to_stale_percent_when_budget_underivable():
    # Controller says 65% spent but the ledger shows nothing up to updated_at
    # (pruned) -> no budget derivable, so judge on the stale 65% at 50% elapsed
    # -> projected 130% -> still trips on stale data (better than going blind).
    v = cb.evaluate(
        spent_percent=65.0,
        live_tokens=0,
        snapshot_tokens=0,
        window_start=NOW - WEEK / 2,
        reset_at=NOW + WEEK / 2,
        now=NOW,
    )
    assert v["live_derived"] is False
    assert v["live_spent_percent"] == pytest.approx(65.0)
    assert v["state"] == "open"


def test_hard_spent_trips_without_a_window():
    # No window bounds, but the stale spent-% is near exhaustion -> still trips
    # via the HARD_SPENT path (projection unavailable, elapsed None).
    v = cb.evaluate(
        spent_percent=98.0,
        live_tokens=None,
        snapshot_tokens=None,
        window_start=None,
        reset_at=None,
        now=NOW,
    )
    assert v["state"] == "open"
    assert v["projected_spent_percent"] is None
    assert v["elapsed_fraction"] is None


# ---------------------------------------------------------------------------
# Explicit budget override (used by the nested 5h-session window)
# ---------------------------------------------------------------------------

def test_budget_tokens_overrides_calibration():
    # budget forced to 400 -> 200 live is 50% (not the 20% the 500/50%
    # calibration would imply). The override wins and still counts as derived.
    v = _eval(live_tokens=200, snapshot_tokens=500, budget_tokens=400.0)
    assert v["implied_budget_tokens"] == 400
    assert v["live_spent_percent"] == pytest.approx(50.0)
    assert v["live_derived"] is True


def test_budget_tokens_ignored_when_non_positive():
    # A zero/negative override is no override -> fall back to the calibration.
    v = _eval(live_tokens=200, snapshot_tokens=500, spent_percent=50.0, budget_tokens=0.0)
    assert v["implied_budget_tokens"] == 1000  # 500 / 50%
    assert v["live_spent_percent"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Burndown — under-utilizing in the tail of the window
# ---------------------------------------------------------------------------

def test_burndown_when_underutilizing_in_tail():
    # 95% elapsed, only 50% spent -> projected ~53% by reset: budget would be
    # left on the table, so drain it. No cap.
    v = cb.evaluate(
        spent_percent=None,
        live_tokens=500,
        snapshot_tokens=None,
        window_start=NOW - WEEK * 0.95,
        reset_at=NOW + WEEK * 0.05,
        now=NOW,
        budget_tokens=1000.0,
    )
    assert v["state"] == "burndown"
    assert v["burning_down"] is True
    assert v["recommended_agent_limit"] is None
    assert v["projected_spent_percent"] == pytest.approx(52.6, abs=0.2)


def test_no_burndown_before_the_tail():
    # Same under-utilization at mid-window is just "closed" — plenty of window
    # left to spend the rest at pace.
    v = cb.evaluate(
        spent_percent=None,
        live_tokens=300,
        snapshot_tokens=None,
        window_start=NOW - WEEK * 0.5,
        reset_at=NOW + WEEK * 0.5,
        now=NOW,
        budget_tokens=1000.0,
    )
    assert v["state"] == "closed"


def test_no_burndown_when_on_pace_in_tail():
    # 95% elapsed and 90% spent -> projected ~95%: reset will land near-full,
    # nothing meaningful to burn down -> closed, not burndown.
    v = cb.evaluate(
        spent_percent=None,
        live_tokens=900,
        snapshot_tokens=None,
        window_start=NOW - WEEK * 0.95,
        reset_at=NOW + WEEK * 0.05,
        now=NOW,
        budget_tokens=1000.0,
    )
    assert v["state"] == "closed"


# ---------------------------------------------------------------------------
# aggregate() — folding nested windows into the board-effective verdict
# ---------------------------------------------------------------------------

def test_aggregate_open_halts_over_everything():
    eff = cb.aggregate([{"state": "burndown", "reason": "b"}, {"state": "open", "reason": "wall"}])
    assert eff["state"] == "open"
    assert eff["tripped"] is True
    assert eff["recommended_agent_limit"] == 0
    assert eff["reason"] == "wall"


def test_aggregate_burndown_lifts_a_throttle():
    # Doctrine: the tail of a window is drained even when another window would
    # throttle the pocket -> burndown wins over half_open, cap released.
    eff = cb.aggregate([{"state": "half_open", "reason": "ahead"}, {"state": "burndown", "reason": "drain"}])
    assert eff["state"] == "burndown"
    assert eff["burning_down"] is True
    assert eff["recommended_agent_limit"] is None
    assert eff["reason"] == "drain"


def test_aggregate_takes_the_tighter_throttle():
    eff = cb.aggregate([{"state": "half_open", "reason": "ahead"}, {"state": "closed", "reason": "on pace"}])
    assert eff["state"] == "half_open"
    assert eff["recommended_agent_limit"] == 1


def test_aggregate_all_unknown_fails_open():
    eff = cb.aggregate([{"state": "unknown"}, {"state": "unknown"}])
    assert eff["state"] == "unknown"
    assert eff["tripped"] is False
    assert eff["recommended_agent_limit"] is None


def test_aggregate_empty_fails_open():
    assert cb.aggregate([])["state"] == "unknown"


def test_aggregate_keeps_component_windows():
    a = {"state": "closed", "reason": "on pace"}
    b = {"state": "burndown", "reason": "drain"}
    assert cb.aggregate([a, b])["windows"] == [a, b]


# ---------------------------------------------------------------------------
# Graceful admission + idle override (task t_4ee09bd0)
# ---------------------------------------------------------------------------

def test_idle_recent_rate_overrides_lifetime_projection():
    """The operator's kimi case: 16% spent at 11% elapsed (onboarding burst
    yesterday) -> lifetime average projects 145% (would trip), but the recent
    burn rate is 0 (idle). The idle override holds the projection at current
    spend so the pocket doesn't self-lock."""
    # budget 1000 (160 tokens / 16%); 16% spent at 11% elapsed.
    v = cb.evaluate(
        spent_percent=16.0,
        live_tokens=160,
        snapshot_tokens=160,  # 160/16% = 1000 budget -> live 16%
        window_start=NOW - WEEK * 0.11,
        reset_at=NOW + WEEK * 0.89,
        now=NOW,
        recent_rate_per_sec=0.0,  # idle
    )
    assert v["idle"] is True
    # Projection held at current spend (16%), not extrapolated to ~145%.
    assert v["projected_spent_percent"] == pytest.approx(16.0)
    assert v["state"] != "open"
    assert v["tripped"] is False
    # No graceful cap either — nothing is burning.
    assert v["sustainable_rate_ratio"] is None


def test_active_recent_rate_keeps_lifetime_projection():
    """A pocket that IS actively burning keeps the lifetime-average projection
    (its real pace); the recent rate drives the graceful-admission ratio
    instead of overriding the projection. A genuine overshoot still trips."""
    # 65% spent at 50% elapsed -> lifetime projects 130% (TRIP). Supply a
    # recent rate that is NOT idle (would add >10% over the rest of the window).
    rate = 100.0  # tokens/sec
    v = _eval(live_tokens=650, recent_rate_per_sec=rate)
    # 650 live, budget 1000; 50% of week left = ~302400 s; growth = huge -> not idle.
    assert v["idle"] is False
    assert v["projected_spent_percent"] == pytest.approx(130.0)  # lifetime avg
    assert v["state"] == "open"


def test_hard_block_only_for_cooldown_or_near_exhaustion():
    """Projection-only overshoot is NOT a hard block (it paces gracefully);
    only cooling or >= HARD_SPENT_PERCENT hard-blocks."""
    # Projection overshoot to 130% but only 65% actually spent -> not hard block.
    v = _eval(live_tokens=650)
    assert v["state"] == "open"
    assert v["tripped"] is True
    assert v["hard_block"] is False  # projection-only
    # Near-exhaustion -> hard block regardless of projection.
    v2 = cb.evaluate(
        spent_percent=96.0, live_tokens=960, snapshot_tokens=960,
        window_start=NOW - WEEK * 0.95, reset_at=NOW + WEEK * 0.05, now=NOW,
    )
    assert v2["hard_block"] is True
    # Cooling -> hard block.
    v3 = _eval(live_tokens=100, cooling=True)
    assert v3["hard_block"] is True


def test_sustainable_rate_ratio_when_overshooting():
    """A pocket burning at 2x the rate that would land it at 100% by reset
    reports ratio 0.5; on/under pace reports None."""
    # 50% spent at 50% elapsed, 0.5 week left. Remaining budget = 50% = 500 tok.
    # Target rate = 500 / (0.5 week). Burn at 2x that -> ratio 0.5.
    seconds_left = WEEK * 0.5
    budget = 1000.0
    remaining = budget * 0.5
    target_rate = remaining / seconds_left
    v = _eval(live_tokens=500, recent_rate_per_sec=target_rate * 2.0)
    assert v["sustainable_rate_ratio"] == pytest.approx(0.5, abs=0.01)


def test_sustainable_rate_ratio_none_on_under_pace():
    """A pocket under its sustainable rate (ratio > 1) emits no cap."""
    seconds_left = WEEK * 0.5
    budget = 1000.0
    remaining = budget * 0.5  # 50% spent
    target_rate = remaining / seconds_left
    # Burning at HALF the target rate -> ratio 2.0 -> on/under pace -> None.
    v = _eval(live_tokens=500, recent_rate_per_sec=target_rate * 0.5)
    assert v["sustainable_rate_ratio"] is None


def test_admission_agent_limit_scales_concurrency():
    """ratio 0.5 of 4 in flight -> cap 2; ratio 0.34 -> floored? no, round=1."""
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=0.5, base_in_flight=4) == 2
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=0.34, base_in_flight=4) == 1


def test_admission_agent_limit_floors_to_one():
    """Even a pocket well over its rate keeps 1 worker (trickle, no self-lock)."""
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=0.01, base_in_flight=10) == 1


def test_admission_agent_limit_none_when_no_ratio():
    """No ratio (on/under pace / idle / burning down) -> no cap."""
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=None, base_in_flight=4) is None
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=1.5, base_in_flight=4) is None  # on/under pace


def test_admission_agent_limit_none_when_cap_not_below_base():
    """If the computed cap is >= the current in-flight, there's nothing to
    throttle -> None."""
    # ratio 0.9, base 4 -> round(3.6)=4 >= 4 -> no cap.
    assert cb.admission_agent_limit(
        sustainable_rate_ratio=0.9, base_in_flight=4) is None


def test_aggregate_takes_min_ratio_and_any_hard_block():
    """Folding windows: any hard_block wins; sustainable_rate_ratio is the min."""
    eff = cb.aggregate([
        {"state": "half_open", "reason": "ahead", "hard_block": False,
         "sustainable_rate_ratio": 0.5, "recent_rate_per_sec": 10.0},
        {"state": "closed", "reason": "ok", "hard_block": True,
         "sustainable_rate_ratio": 0.8, "recent_rate_per_sec": 5.0},
    ])
    assert eff["hard_block"] is True
    assert eff["sustainable_rate_ratio"] == 0.5  # min
    assert eff["recent_rate_per_sec"] == 10.0  # max


def test_aggregate_burndown_lifts_sustainable_cap():
    """A pocket burning down (tail of a window, under-utilizing) is not rate-
    capped — the doctrine says drain the remainder, so sustainable -> None."""
    eff = cb.aggregate([
        {"state": "half_open", "reason": "ahead", "hard_block": False,
         "sustainable_rate_ratio": 0.5, "recent_rate_per_sec": 10.0},
        {"state": "burndown", "reason": "drain", "hard_block": False,
         "sustainable_rate_ratio": None, "recent_rate_per_sec": 5.0},
    ])
    assert eff["state"] == "burndown"
    assert eff["sustainable_rate_ratio"] is None  # lifted

