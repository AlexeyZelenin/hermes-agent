"""Tests for the payment-source-aware spend budget guard."""

from types import SimpleNamespace

import pytest

from agent.budget_guard import (
    EXEMPT_PROTOCOL_TOOLS,
    BudgetGuard,
    BudgetGuardConfig,
    classify_spend_source,
    feed_budget_guard,
)
from agent.usage_pricing import CanonicalUsage, estimate_metered_cost


# ── classify_spend_source ────────────────────────────────────────────────────


def test_included_status_is_subscription():
    assert classify_spend_source("anthropic", "included") == "subscription"


def test_subscription_provider_is_subscription_even_when_unpriced():
    assert classify_spend_source("openai-codex", "unknown") == "subscription"
    assert classify_spend_source("xai-oauth", "estimated") == "subscription"


def test_metered_provider_is_api_key():
    assert classify_spend_source("anthropic", "estimated") == "api_key"
    assert classify_spend_source(None, None) == "api_key"


# ── config ───────────────────────────────────────────────────────────────────


def test_config_defaults_hard_stop_off_and_100_dollar_limit():
    cfg = BudgetGuardConfig()
    assert cfg.daily_limit_usd == 100.0
    assert cfg.hard_stop_enabled is False
    assert cfg.hard_threshold_usd == 100.0
    assert cfg.warn_threshold_usd == 75.0
    assert EXEMPT_PROTOCOL_TOOLS <= cfg.exempt_tools


def test_config_from_mapping_parses_all_fields():
    cfg = BudgetGuardConfig.from_mapping(
        {
            "daily_limit_usd": 50,
            "warn_pct": 0.5,
            "hard_pct": 0.9,
            "hard_stop_enabled": "true",
            "warnings_enabled": False,
            "exempt_tools": ["kanban_block", "custom_tool"],
        }
    )
    assert cfg.daily_limit_usd == 50.0
    assert cfg.hard_threshold_usd == 45.0
    assert cfg.warn_threshold_usd == 25.0
    assert cfg.hard_stop_enabled is True
    assert cfg.warnings_enabled is False
    assert cfg.exempt_tools == frozenset({"kanban_block", "custom_tool"})


def test_config_from_mapping_ignores_garbage():
    cfg = BudgetGuardConfig.from_mapping(None)
    assert cfg == BudgetGuardConfig()
    cfg2 = BudgetGuardConfig.from_mapping({"daily_limit_usd": "not-a-number"})
    assert cfg2.daily_limit_usd == 100.0


# ── record_spend accounting ──────────────────────────────────────────────────


def test_record_spend_splits_by_source():
    guard = BudgetGuard()
    guard.record_spend(10.0, "api_key")
    guard.record_spend(5.0, "subscription")
    guard.record_spend(2.0, "api_key")
    assert guard.api_key_spend_usd == pytest.approx(12.0)
    assert guard.subscription_spend_usd == pytest.approx(5.0)


def test_record_spend_clamps_negative_and_none():
    guard = BudgetGuard()
    guard.record_spend(-5.0, "api_key")
    guard.record_spend(None, "api_key")
    assert guard.api_key_spend_usd == 0.0


# ── hard stop: api-key spend, opt-in ─────────────────────────────────────────


def test_hard_stop_disabled_by_default_never_blocks():
    guard = BudgetGuard(BudgetGuardConfig(daily_limit_usd=10.0))
    guard.record_spend(1000.0, "api_key")
    decision = guard.before_call("terminal")
    assert decision.allows_execution is True


def test_hard_stop_blocks_api_key_over_limit_when_enabled():
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=10.0, hard_stop_enabled=True)
    )
    guard.record_spend(10.0, "api_key")
    decision = guard.before_call("terminal")
    assert decision.allows_execution is False
    assert decision.should_halt is True
    assert decision.code == "budget_hard_stop"


def test_hard_stop_allows_api_key_below_limit():
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=10.0, hard_stop_enabled=True)
    )
    guard.record_spend(9.99, "api_key")
    assert guard.before_call("terminal").allows_execution is True


# ── subscription spend never hard-stops ──────────────────────────────────────


def test_subscription_spend_never_blocks_even_over_limit():
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=10.0, hard_stop_enabled=True)
    )
    guard.record_spend(1000.0, "subscription")
    decision = guard.before_call("terminal")
    assert decision.allows_execution is True


# ── exempt protocol tools bypass hard stop ───────────────────────────────────


@pytest.mark.parametrize("tool", sorted(EXEMPT_PROTOCOL_TOOLS))
def test_exempt_protocol_tools_bypass_hard_stop(tool):
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=10.0, hard_stop_enabled=True)
    )
    guard.record_spend(1000.0, "api_key")
    # Non-exempt tool is blocked...
    assert guard.before_call("terminal").allows_execution is False
    # ...but the protocol lifeline stays open.
    assert guard.before_call(tool).allows_execution is True


# ── soft warnings ────────────────────────────────────────────────────────────


def test_api_key_warning_fires_once_at_threshold():
    guard = BudgetGuard(BudgetGuardConfig(daily_limit_usd=100.0, warn_pct=0.75))
    guard.record_spend(80.0, "api_key")
    first = guard.before_call("terminal")
    assert first.action == "warn"
    assert first.code == "budget_api_key_warning"
    # Subsequent calls do not re-warn for the same source.
    assert guard.before_call("terminal").action == "allow"


def test_subscription_warning_is_warn_only():
    guard = BudgetGuard(BudgetGuardConfig(daily_limit_usd=100.0, warn_pct=0.75))
    guard.record_spend(90.0, "subscription")
    decision = guard.before_call("terminal")
    assert decision.action == "warn"
    assert decision.code == "budget_subscription_warning"
    assert decision.allows_execution is True


def test_warnings_can_be_disabled():
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=100.0, warnings_enabled=False)
    )
    guard.record_spend(90.0, "api_key")
    assert guard.before_call("terminal").action == "allow"


def test_exempt_tool_stays_quiet_at_warn_threshold():
    guard = BudgetGuard(BudgetGuardConfig(daily_limit_usd=100.0, warn_pct=0.75))
    guard.record_spend(90.0, "api_key")
    assert guard.before_call("kanban_comment").action == "allow"


# ── feed_budget_guard ────────────────────────────────────────────────────────


def test_feed_none_guard_is_noop():
    feed_budget_guard(
        None,
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="",
        api_key="",
        usage=CanonicalUsage(input_tokens=1000),
        cost_result=SimpleNamespace(status="estimated", amount_usd=1.0),
    )  # must not raise


def test_feed_api_key_records_real_amount():
    guard = BudgetGuard()
    feed_budget_guard(
        guard,
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="",
        api_key="",
        usage=CanonicalUsage(input_tokens=1000),
        cost_result=SimpleNamespace(status="estimated", amount_usd=3.5),
    )
    assert guard.api_key_spend_usd == pytest.approx(3.5)
    assert guard.subscription_spend_usd == 0.0


def test_feed_subscription_records_shadow_price_not_zero():
    # A subscription turn reports amount_usd=0 / status=included, but the guard
    # should record the would-be-metered shadow cost so warnings are meaningful.
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    shadow = estimate_metered_cost(
        "claude-opus-4-8", usage, provider="anthropic", base_url="", api_key=""
    )
    assert shadow.amount_usd is not None and shadow.amount_usd > 0

    guard = BudgetGuard()
    feed_budget_guard(
        guard,
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="",
        api_key="",
        usage=usage,
        cost_result=SimpleNamespace(status="included", amount_usd=0.0),
    )
    assert guard.subscription_spend_usd == pytest.approx(float(shadow.amount_usd))
    assert guard.api_key_spend_usd == 0.0


def test_estimate_metered_cost_prices_claude_opus():
    # Opus 4.8: $5/M in, $25/M out.
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = estimate_metered_cost(
        "claude-opus-4-8", usage, provider="anthropic", base_url="", api_key=""
    )
    assert result.amount_usd == pytest.approx(30.0)


# ── executor wiring: _budget_guard_block ─────────────────────────────────────


def _guarded_agent(**cfg_kwargs):
    return SimpleNamespace(_budget_guard=BudgetGuard(BudgetGuardConfig(**cfg_kwargs)))


def test_executor_helper_returns_block_for_over_limit_api_key():
    from agent.tool_executor import _budget_guard_block

    agent = _guarded_agent(daily_limit_usd=10.0, hard_stop_enabled=True)
    agent._budget_guard.record_spend(20.0, "api_key")
    decision = _budget_guard_block(agent, "terminal", {})
    assert decision is not None and decision.should_halt


def test_executor_helper_lets_exempt_tool_through_over_limit():
    from agent.tool_executor import _budget_guard_block

    agent = _guarded_agent(daily_limit_usd=10.0, hard_stop_enabled=True)
    agent._budget_guard.record_spend(20.0, "api_key")
    assert _budget_guard_block(agent, "kanban_block", {}) is None


def test_executor_helper_allows_and_logs_warning(caplog):
    from agent.tool_executor import _budget_guard_block

    agent = _guarded_agent(daily_limit_usd=100.0, warn_pct=0.75)
    agent._budget_guard.record_spend(90.0, "api_key")
    with caplog.at_level("WARNING"):
        assert _budget_guard_block(agent, "terminal", {}) is None
    assert any("Budget guard" in r.message for r in caplog.records)


def test_executor_helper_noop_without_guard():
    from agent.tool_executor import _budget_guard_block

    assert _budget_guard_block(SimpleNamespace(_budget_guard=None), "terminal", {}) is None
    assert _budget_guard_block(SimpleNamespace(), "terminal", {}) is None
