"""Data-driven multi-vendor model grid + price-vs-suitability router.

The grid (`website/static/api/model-grid.json`) is a machine-readable table of
non-frontier vendor models (DeepSeek / Gemini / Grok / GLM / Qwen) tagged with a
capability class (cheap / aux / mid / niche), per-1M-token prices, subscription
info, and independent-leaderboard benchmark numbers. Every benchmark number in
the grid carries a per-number source; gaps are explicit ``null`` (a missing
number is never filled from a different leaderboard or a different model
version). This module loads that table and picks a vendor/model for a task class
by a transparent, logged rule so premium (Claude/ChatGPT) tokens can be spent
only where they are actually needed.

The rule is deliberately single-axis to respect the research card's warning that
numbers must not be merged across leaderboards:

* **metered mode** (``prefer_subscription=False``, the default): among models of
  the class that have BOTH a metered price and a SWE-bench Verified score, pick
  the best score-per-dollar (``swe_bench_verified / blended_price``). This is the
  classic price-quality frontier pick and is what saves premium tokens.
* **subscription mode** (``prefer_subscription=True``, or when metered mode has
  no candidate): among subscription-backed models of the class, rank by the
  first benchmark all candidates can be compared on WITHOUT crossing
  leaderboards — SWE-bench Verified first, else LMArena Elo, else SWE-bench Pro —
  and pick the best; a flat-fee plan is predictable for a large agent fleet.

Both modes emit a human-readable ``rationale`` naming the rule, the chosen
model, the deciding number and its source, so the decision is auditable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Repo root: hermes_cli/model_grid.py -> <root>. Mirrors hermes_cli.config's
# project-root convention and the model-catalog checkout path.
_GRID_PATH = Path(__file__).parent.parent / "website" / "static" / "api" / "model-grid.json"

# Blend weights for the metered price axis. Agent traffic is a mix of prompt and
# completion tokens; a symmetric blend keeps the cost signal simple and its
# meaning obvious in the logged rationale. Documented here so the number in the
# rationale is reproducible.
_BLEND_W_IN = 0.5
_BLEND_W_OUT = 0.5

# Grid classes, cheapest/lightest first. ``niche`` is catalogued for reference
# but excluded from default routing (no canonical-leaderboard suitability).
_ROUTABLE_CLASSES = ("cheap", "aux", "mid")

# How a kanban decompose tier maps onto a grid class for the advisory pick. Only
# the offload-worthy tiers are mapped; ``standard`` (Tier-2 workhorse) and
# ``strong`` (frontier) deliberately have no vendor suggestion.
DECOMPOSE_TIER_TO_CLASS = {"cheap": "cheap", "mid": "mid"}


@dataclass(frozen=True)
class GridModel:
    """One vendor model row from the grid."""

    id: str
    catalog_id: str
    vendor: str
    provider: str
    grid_class: str
    price_in: Optional[float]
    price_out: Optional[float]
    subscription: bool
    subscription_plan: Optional[str]
    swe_bench_verified: Optional[float]
    swe_bench_pro: Optional[float]
    lmarena_elo: Optional[float]
    sources: dict
    local: bool = False

    def blended_price(self) -> Optional[float]:
        """Weighted metered $/1M-tok, or None when either side is unpriced."""
        if self.price_in is None or self.price_out is None:
            return None
        return _BLEND_W_IN * self.price_in + _BLEND_W_OUT * self.price_out


@dataclass(frozen=True)
class RouteDecision:
    """The router's transparent verdict for a task class."""

    task_class: str
    mode: str                    # "metered" | "subscription"
    vendor: str
    provider: str
    model: str
    catalog_id: str
    score: float                 # the ranking score under ``mode``
    suitability: float           # the deciding benchmark value
    suitability_metric: str      # e.g. "swe_bench_verified"
    suitability_source: str      # provenance of the deciding number
    blended_price: Optional[float]
    rationale: str


def _coerce_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_grid(path: Optional[Path] = None) -> list[GridModel]:
    """Load the model grid, fail-open to an empty list.

    A missing/invalid grid file yields ``[]`` (like the rest of Hermes' data
    reads) so an advisory router never breaks a caller.
    """
    grid_path = path or _GRID_PATH
    try:
        raw = json.loads(grid_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("model_grid: grid unavailable at %s: %s", grid_path, exc)
        return []
    models: list[GridModel] = []
    for entry in raw.get("models", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        models.append(
            GridModel(
                id=str(entry["id"]),
                catalog_id=str(entry.get("catalog_id") or entry["id"]),
                vendor=str(entry.get("vendor") or ""),
                provider=str(entry.get("provider") or ""),
                grid_class=str(entry.get("class") or ""),
                price_in=_coerce_float(entry.get("price_in_per_mtok")),
                price_out=_coerce_float(entry.get("price_out_per_mtok")),
                subscription=bool(entry.get("subscription")),
                subscription_plan=entry.get("subscription_plan"),
                swe_bench_verified=_coerce_float(entry.get("swe_bench_verified")),
                swe_bench_pro=_coerce_float(entry.get("swe_bench_pro")),
                lmarena_elo=_coerce_float(entry.get("lmarena_elo")),
                sources=entry.get("sources") or {},
                local=bool(entry.get("local")),
            )
        )
    return models


def _local_pick(candidates: list[GridModel]) -> Optional[RouteDecision]:
    """Prefer a locally-hosted model — it costs zero subscription/metered tokens.

    A local OpenAI-compatible endpoint (Ollama / MLX / LM Studio) runs on the
    operator's own hardware, so for the offload-worthy classes (cheap / aux) it
    beats every paid vendor on cost by definition. When several local models are
    tagged for a class, the highest SWE-bench Verified wins (else stable id) so
    the pick is deterministic and auditable. Returns ``None`` when the class has
    no local model, letting the paid metered/subscription rules take over.
    """
    locals_ = [m for m in candidates if m.local]
    if not locals_:
        return None
    locals_.sort(key=lambda m: (-(m.swe_bench_verified or 0.0), m.id))
    m = locals_[0]
    suit = m.swe_bench_verified
    metric = "swe_bench_verified" if suit is not None else "none"
    src = m.sources.get("swe_bench_verified", "unknown") if suit is not None else "local"
    suit_txt = f"SWE-V {suit:.1f}%" if suit is not None else "unbenchmarked"
    rationale = (
        f"class={m.grid_class} rule=local:zero-cost -> "
        f"{m.vendor}/{m.id} ({suit_txt}; runs on local hardware, "
        f"0 subscription/metered tokens; src: {src})"
    )
    return RouteDecision(
        task_class=m.grid_class, mode="local", vendor=m.vendor,
        provider=m.provider, model=m.id, catalog_id=m.catalog_id,
        score=0.0, suitability=suit if suit is not None else 0.0,
        suitability_metric=metric, suitability_source=src,
        blended_price=0.0, rationale=rationale,
    )


def _metered_pick(candidates: list[GridModel],
                  budget_ceiling: Optional[float]) -> Optional[RouteDecision]:
    """Best score-per-dollar among priced + SWE-V-scored models."""
    ranked: list[tuple[float, float, GridModel]] = []
    for m in candidates:
        price = m.blended_price()
        if price is None or price <= 0 or m.swe_bench_verified is None:
            continue
        if budget_ceiling is not None and price > budget_ceiling:
            continue
        score = m.swe_bench_verified / price
        ranked.append((score, m.swe_bench_verified, m))
    if not ranked:
        return None
    # Best score-per-dollar; tie-break higher raw suitability, then stable id.
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2].id))
    score, suit, m = ranked[0]
    src = m.sources.get("swe_bench_verified", "unknown")
    price = m.blended_price()
    rationale = (
        f"class={m.grid_class} rule=metered:max(SWE-bench-Verified/$blend) -> "
        f"{m.vendor}/{m.id} (SWE-V {suit:.1f}% @ ${price:.3f}/M blend = "
        f"{score:.1f} pts/$; SWE-V src: {src})"
    )
    return RouteDecision(
        task_class=m.grid_class, mode="metered", vendor=m.vendor,
        provider=m.provider, model=m.id, catalog_id=m.catalog_id,
        score=round(score, 3), suitability=suit,
        suitability_metric="swe_bench_verified", suitability_source=src,
        blended_price=price, rationale=rationale,
    )


def _subscription_pick(candidates: list[GridModel]) -> Optional[RouteDecision]:
    """Highest suitability among subscription models on a single benchmark.

    Tries SWE-bench Verified, then LMArena Elo, then SWE-bench Pro — never
    comparing two models across different benchmarks.
    """
    subs = [m for m in candidates if m.subscription]
    for metric, getter in (
        ("swe_bench_verified", lambda m: m.swe_bench_verified),
        ("lmarena_elo", lambda m: m.lmarena_elo),
        ("swe_bench_pro", lambda m: m.swe_bench_pro),
    ):
        scored = [(getter(m), m) for m in subs if getter(m) is not None]
        if not scored:
            continue
        scored.sort(key=lambda t: (-t[0], t[1].id))
        suit, m = scored[0]
        src = m.sources.get(metric, "unknown")
        plan = m.subscription_plan or "subscription"
        rationale = (
            f"class={m.grid_class} rule=subscription:max({metric}) -> "
            f"{m.vendor}/{m.id} ({metric.replace('_', '-')} {suit:g}; "
            f"plan: {plan}; src: {src})"
        )
        return RouteDecision(
            task_class=m.grid_class, mode="subscription", vendor=m.vendor,
            provider=m.provider, model=m.id, catalog_id=m.catalog_id,
            score=suit, suitability=suit, suitability_metric=metric,
            suitability_source=src, blended_price=m.blended_price(),
            rationale=rationale,
        )
    return None


def route(task_class: str, *,
          prefer_subscription: bool = False,
          allow_local: bool = True,
          budget_ceiling: Optional[float] = None,
          grid: Optional[list[GridModel]] = None) -> Optional[RouteDecision]:
    """Pick a vendor/model for ``task_class`` by price vs. suitability.

    A locally-hosted model tagged for the class wins by default (``allow_local``)
    because it costs zero subscription/metered tokens — this is the cheap/aux
    offload path. Pass ``allow_local=False`` to force a paid vendor (e.g. when the
    local endpoint is known-down and the caller wants the grid's cloud pick).

    Returns ``None`` when the class is not routable or has no rankable model.
    The chosen decision (or the no-pick) is logged so the rule is auditable.
    """
    cls = (task_class or "").strip().lower()
    if cls not in _ROUTABLE_CLASSES:
        logger.debug("model_grid.route: class %r not routable", task_class)
        return None
    models = grid if grid is not None else load_grid()
    candidates = [m for m in models if m.grid_class == cls]
    if not candidates:
        logger.debug("model_grid.route: no grid models for class %s", cls)
        return None

    decision: Optional[RouteDecision] = None
    if allow_local:
        decision = _local_pick(candidates)
    if decision is None:
        if prefer_subscription:
            decision = _subscription_pick(candidates) or _metered_pick(candidates, budget_ceiling)
        else:
            decision = _metered_pick(candidates, budget_ceiling) or _subscription_pick(candidates)

    if decision is None:
        logger.info("model_grid.route: class=%s no rankable model (no priced+scored "
                    "or subscription candidate)", cls)
        return None
    logger.info("model_grid.route: %s", decision.rationale)
    return decision


def route_for_decompose_tier(tier: str, **kwargs) -> Optional[RouteDecision]:
    """Advisory router for a kanban decompose tier (cheap/mid only)."""
    cls = DECOMPOSE_TIER_TO_CLASS.get((tier or "").strip().lower())
    if cls is None:
        return None
    return route(cls, **kwargs)
