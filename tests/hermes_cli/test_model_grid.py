"""Tests for hermes_cli.model_grid — the data-driven vendor router.

Covers the shipped grid data (schema + source-per-number integrity) and the
price-vs-suitability routing rule on isolated in-memory grids so a routing
assertion never depends on the exact shipped numbers.
"""

from __future__ import annotations

import logging

import pytest

from hermes_cli import model_grid
from hermes_cli.model_grid import GridModel, route


def _m(**kw) -> GridModel:
    """Build a GridModel with sane defaults; override fields per test."""
    base = dict(
        id="m", catalog_id="v/m", vendor="v", provider="v", grid_class="cheap",
        price_in=None, price_out=None, subscription=False, subscription_plan=None,
        swe_bench_verified=None, swe_bench_pro=None, lmarena_elo=None, sources={},
    )
    base.update(kw)
    return GridModel(**base)


# ── Shipped grid data integrity ──────────────────────────────────────────────

def test_shipped_grid_loads_and_is_nonempty():
    models = model_grid.load_grid()
    assert models, "shipped model-grid.json should load at least one model"
    # The five researched vendors must all appear.
    vendors = {m.vendor for m in models}
    assert {"deepseek", "gemini", "grok", "glm", "qwen"} <= vendors


def test_every_benchmark_number_has_a_source():
    """AC #4: each suitability number must carry a recorded source."""
    for m in model_grid.load_grid():
        if m.swe_bench_verified is not None:
            assert m.sources.get("swe_bench_verified"), f"{m.id} SWE-V lacks source"
        if m.lmarena_elo is not None:
            assert m.sources.get("lmarena_elo"), f"{m.id} LMArena lacks source"
        if m.swe_bench_pro is not None:
            assert m.sources.get("swe_bench_pro"), f"{m.id} SWE-Pro lacks source"
        if m.price_in is not None or m.price_out is not None:
            assert m.sources.get("price"), f"{m.id} price lacks source"


def test_load_grid_fail_open_on_missing_file(tmp_path):
    assert model_grid.load_grid(tmp_path / "does-not-exist.json") == []


# ── Metered routing rule ─────────────────────────────────────────────────────

def test_metered_pick_maximizes_score_per_dollar():
    grid = [
        _m(id="cheap-good", vendor="a", price_in=0.14, price_out=0.28,
           swe_bench_verified=79.0, sources={"swe_bench_verified": "s", "price": "p"}),
        _m(id="pricey-good", vendor="b", price_in=2.0, price_out=12.0,
           swe_bench_verified=80.6, sources={"swe_bench_verified": "s", "price": "p"}),
    ]
    d = route("cheap", grid=grid)
    assert d is not None
    assert d.mode == "metered"
    assert d.model == "cheap-good"          # 79/0.21 >> 80.6/7.0
    assert d.suitability_metric == "swe_bench_verified"
    assert d.suitability_source == "s"


def test_metered_skips_unpriced_or_unscored_models():
    grid = [
        _m(id="no-price", swe_bench_verified=90.0, sources={"swe_bench_verified": "s"}),
        _m(id="no-score", price_in=0.1, price_out=0.1, sources={"price": "p"}),
    ]
    # Neither is metered-rankable; falls through to subscription mode → none here.
    assert route("cheap", grid=grid) is None


def test_budget_ceiling_filters_out_expensive_models():
    grid = [
        _m(id="cheap", price_in=0.1, price_out=0.1, swe_bench_verified=70.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
        _m(id="rich", price_in=5.0, price_out=5.0, swe_bench_verified=99.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
    ]
    d = route("cheap", grid=grid, budget_ceiling=1.0)
    assert d is not None and d.model == "cheap"   # blended 5.0 > ceiling excludes "rich"


# ── Subscription routing rule ────────────────────────────────────────────────

def test_subscription_pick_ranks_within_single_benchmark():
    grid = [
        _m(id="glm", grid_class="aux", subscription=True, subscription_plan="GLM",
           swe_bench_verified=77.8, sources={"swe_bench_verified": "s"}),
        _m(id="qwen", grid_class="aux", subscription=True, subscription_plan="Qwen",
           swe_bench_verified=78.8, sources={"swe_bench_verified": "s"}),
    ]
    d = route("aux", grid=grid, prefer_subscription=True)
    assert d is not None
    assert d.mode == "subscription"
    assert d.model == "qwen"                 # higher SWE-V
    assert d.suitability_metric == "swe_bench_verified"


def test_subscription_falls_to_next_metric_when_swe_v_absent():
    grid = [
        _m(id="a", grid_class="aux", subscription=True, lmarena_elo=1465,
           sources={"lmarena_elo": "s"}),
        _m(id="b", grid_class="aux", subscription=True, lmarena_elo=1480,
           sources={"lmarena_elo": "s"}),
    ]
    d = route("aux", grid=grid, prefer_subscription=True)
    assert d is not None and d.model == "b" and d.suitability_metric == "lmarena_elo"


def test_metered_mode_falls_back_to_subscription_when_no_priced_candidate():
    grid = [
        _m(id="sub-only", grid_class="aux", subscription=True,
           swe_bench_verified=78.0, sources={"swe_bench_verified": "s"}),
    ]
    d = route("aux", grid=grid)  # default metered, but nothing priced → subscription
    assert d is not None and d.mode == "subscription" and d.model == "sub-only"


# ── Class handling + decompose bridge ────────────────────────────────────────

def test_niche_class_is_not_routable():
    grid = [_m(id="grok", grid_class="niche", price_in=2.0, price_out=6.0,
               swe_bench_verified=86.0, sources={"swe_bench_verified": "s", "price": "p"})]
    assert route("niche", grid=grid) is None


def test_unknown_class_returns_none():
    assert route("frontier", grid=[]) is None


def test_decompose_tier_maps_only_cheap_and_mid():
    assert model_grid.route_for_decompose_tier("standard") is None
    assert model_grid.route_for_decompose_tier("strong") is None
    # cheap/mid map onto real classes (uses shipped grid).
    assert model_grid.route_for_decompose_tier("cheap") is not None
    assert model_grid.route_for_decompose_tier("mid") is not None


def test_decision_is_logged(caplog):
    grid = [_m(id="x", price_in=0.1, price_out=0.1, swe_bench_verified=70.0,
               sources={"swe_bench_verified": "s", "price": "p"})]
    with caplog.at_level(logging.INFO, logger="hermes_cli.model_grid"):
        route("cheap", grid=grid)
    assert any("model_grid.route" in r.message for r in caplog.records)


def test_routing_is_deterministic_on_ties():
    grid = [
        _m(id="b-id", price_in=0.1, price_out=0.1, swe_bench_verified=70.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
        _m(id="a-id", price_in=0.1, price_out=0.1, swe_bench_verified=70.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
    ]
    # Identical score+suitability → stable tie-break by id ("a-id" < "b-id").
    d = route("cheap", grid=grid)
    assert d is not None and d.model == "a-id"


# ── Local (zero-token) routing rule — task t_2cfa06c1 ────────────────────────

def test_local_model_wins_cheap_over_priced_vendor():
    """A local model is free, so it beats any paid vendor on the cheap tier."""
    grid = [
        _m(id="deepseek", price_in=0.14, price_out=0.28, swe_bench_verified=79.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
        _m(id="qwen3-coder:30b", vendor="qwen", provider="local", local=True,
           swe_bench_verified=50.3, sources={"swe_bench_verified": "s"}),
    ]
    d = route("cheap", grid=grid)
    assert d is not None
    assert d.mode == "local"
    assert d.model == "qwen3-coder:30b"
    assert d.provider == "local"
    assert d.blended_price == 0.0            # zero subscription/metered cost


def test_allow_local_false_restores_paid_pick():
    """A known-down local box can be excluded to force the grid's cloud pick."""
    grid = [
        _m(id="deepseek", price_in=0.14, price_out=0.28, swe_bench_verified=79.0,
           sources={"swe_bench_verified": "s", "price": "p"}),
        _m(id="qwen3-coder:30b", provider="local", local=True,
           swe_bench_verified=50.3, sources={"swe_bench_verified": "s"}),
    ]
    d = route("cheap", grid=grid, allow_local=False)
    assert d is not None and d.mode == "metered" and d.model == "deepseek"


def test_local_pick_prefers_higher_swe_v_among_locals():
    grid = [
        _m(id="weak-local", provider="local", local=True, swe_bench_verified=40.0,
           sources={"swe_bench_verified": "s"}),
        _m(id="strong-local", provider="local", local=True, swe_bench_verified=50.3,
           sources={"swe_bench_verified": "s"}),
    ]
    d = route("cheap", grid=grid)
    assert d is not None and d.model == "strong-local"


def test_local_pick_handles_unbenchmarked_model():
    grid = [_m(id="mystery-local", provider="local", local=True)]
    d = route("cheap", grid=grid)
    assert d is not None and d.mode == "local"
    assert d.suitability == 0.0 and d.suitability_metric == "none"


def test_shipped_grid_registers_local_qwen_for_cheap_and_aux():
    """AC #2: the local Qwen3-Coder backend is registered + selected for the
    cheap and aux tiers in the shipped grid."""
    models = model_grid.load_grid()
    local_rows = [m for m in models if m.local]
    assert local_rows, "shipped grid must register at least one local model"
    classes = {m.grid_class for m in local_rows}
    assert {"cheap", "aux"} <= classes
    for cls in ("cheap", "aux"):
        d = route(cls)
        assert d is not None and d.mode == "local" and d.provider == "local"
        assert d.model == "qwen3-coder:30b"
