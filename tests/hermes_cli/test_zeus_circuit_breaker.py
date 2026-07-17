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
