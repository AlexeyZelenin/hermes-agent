"""Tests for the payment-source-aware spend budget guard."""

from types import SimpleNamespace

import pytest

from agent.budget_guard import (
    BUDGET_GUARD_OFF_ENV,
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


def test_config_defaults_fail_closed_with_generous_cap():
    # Fail-closed: hard stop ON by default so an unattended run has a ceiling,
    # with a generous default cap ordinary work will not trip.
    cfg = BudgetGuardConfig()
    assert cfg.hard_stop_enabled is True
    assert cfg.daily_limit_usd == 250.0
    assert cfg.hard_threshold_usd == 250.0
    assert cfg.warn_threshold_usd == 187.5
    assert cfg.metering_dead_after == 3
    assert EXEMPT_PROTOCOL_TOOLS <= cfg.exempt_tools


def test_config_from_mapping_parses_all_fields():
    cfg = BudgetGuardConfig.from_mapping(
        {
            "daily_limit_usd": 50,
            "warn_pct": 0.5,
            "hard_pct": 0.9,
            "hard_stop_enabled": "true",
            "warnings_enabled": False,
            "metering_dead_after": 5,
            "exempt_tools": ["kanban_block", "custom_tool"],
        }
    )
    assert cfg.daily_limit_usd == 50.0
    assert cfg.hard_threshold_usd == 45.0
    assert cfg.warn_threshold_usd == 25.0
    assert cfg.hard_stop_enabled is True
    assert cfg.warnings_enabled is False
    assert cfg.metering_dead_after == 5
    assert cfg.exempt_tools == frozenset({"kanban_block", "custom_tool"})


def test_config_from_mapping_ignores_garbage():
    cfg = BudgetGuardConfig.from_mapping(None)
    assert cfg == BudgetGuardConfig()
    cfg2 = BudgetGuardConfig.from_mapping({"daily_limit_usd": "not-a-number"})
    assert cfg2.daily_limit_usd == 250.0


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


def test_hard_stop_enabled_by_default_blocks_over_cap():
    # Fail-closed default: over the cap, non-exempt work is blocked out of the box.
    guard = BudgetGuard(BudgetGuardConfig(daily_limit_usd=10.0))
    guard.record_spend(1000.0, "api_key")
    decision = guard.before_call("terminal")
    assert decision.allows_execution is False
    assert decision.code == "budget_hard_stop"


def test_hard_stop_explicitly_disabled_never_blocks():
    guard = BudgetGuard(
        BudgetGuardConfig(daily_limit_usd=10.0, hard_stop_enabled=False)
    )
    guard.record_spend(1000.0, "api_key")
    assert guard.before_call("terminal").allows_execution is True


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


# ── per-session override: conscious opt-out ──────────────────────────────────


def test_session_override_disables_hard_stop_and_unblocks():
    # Both directions: default fail-closed blocks at the cap, the env override
    # removes the block for this session.
    cfg = BudgetGuardConfig(daily_limit_usd=10.0)
    blocked = BudgetGuard(cfg)
    blocked.record_spend(50.0, "api_key")
    assert blocked.before_call("terminal").allows_execution is False

    overridden = BudgetGuard(cfg.with_session_overrides({BUDGET_GUARD_OFF_ENV: "1"}))
    overridden.record_spend(50.0, "api_key")
    assert overridden.before_call("terminal").allows_execution is True


def test_session_override_absent_or_falsey_keeps_hard_stop():
    cfg = BudgetGuardConfig(daily_limit_usd=10.0)
    assert cfg.with_session_overrides({}).hard_stop_enabled is True
    assert cfg.with_session_overrides({BUDGET_GUARD_OFF_ENV: "0"}).hard_stop_enabled is True
    assert (
        cfg.with_session_overrides({BUDGET_GUARD_OFF_ENV: "off"}).hard_stop_enabled is True
    )


def test_session_override_reads_process_env(monkeypatch):
    monkeypatch.setenv(BUDGET_GUARD_OFF_ENV, "yes")
    cfg = BudgetGuardConfig(daily_limit_usd=10.0)
    assert cfg.with_session_overrides().hard_stop_enabled is False


# ── metering-dead: broken pricing feed must not read a silent $0 ──────────────


def test_metering_failure_counts_and_resets_on_clean_record():
    guard = BudgetGuard()
    guard.record_metering_failure("boom")
    guard.record_metering_failure("boom")
    assert guard.metering_failures == 2
    # A successful record proves the feed is alive again.
    guard.record_spend(1.0, "api_key")
    assert guard.metering_failures == 0
    assert guard.metering_broken is False


def test_metering_broken_after_threshold_blocks_when_hard_stop_on():
    guard = BudgetGuard(BudgetGuardConfig(metering_dead_after=2))
    guard.record_metering_failure("boom")
    assert guard.metering_broken is False  # one failure is not yet dead
    guard.record_metering_failure("boom")
    assert guard.metering_broken is True
    decision = guard.before_call("terminal")
    assert decision.allows_execution is False
    assert decision.code == "budget_metering_dead"


def test_metering_dead_lets_exempt_tools_through():
    guard = BudgetGuard(BudgetGuardConfig(metering_dead_after=1))
    guard.record_metering_failure("boom")
    assert guard.before_call("terminal").allows_execution is False
    assert guard.before_call("kanban_block").allows_execution is True


def test_metering_dead_warns_only_when_hard_stop_disabled():
    guard = BudgetGuard(
        BudgetGuardConfig(metering_dead_after=1, hard_stop_enabled=False)
    )
    guard.record_metering_failure("boom")
    first = guard.before_call("terminal")
    assert first.action == "warn"
    assert first.code == "budget_metering_dead_warning"
    assert first.allows_execution is True
    # One-time signal, does not repeat every call.
    assert guard.before_call("terminal").action == "allow"


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


def test_feed_unpriced_api_key_turn_flags_metering_not_silent_zero():
    # A None cost on an api-key turn is the silent under-count that would leave
    # the hard stop toothless — it must register as a metering failure, not $0.
    guard = BudgetGuard()
    feed_budget_guard(
        guard,
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="",
        api_key="",
        usage=CanonicalUsage(input_tokens=1000),
        cost_result=SimpleNamespace(status="estimated", amount_usd=None),
    )
    assert guard.metering_failures == 1
    assert guard.api_key_spend_usd == 0.0


def test_feed_exception_reports_metering_failure_and_does_not_raise():
    guard = BudgetGuard()

    class _Boom:
        def __getattr__(self, name):  # any attribute access blows up
            raise RuntimeError("pricing feed dead")

    # Must not raise (loop safety) but must record the failure (not swallow it).
    feed_budget_guard(
        guard,
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="",
        api_key="",
        usage=CanonicalUsage(input_tokens=1000),
        cost_result=_Boom(),
    )
    assert guard.metering_failures == 1


def test_feed_systematic_break_makes_guard_fail_closed():
    # End-to-end: a pricing feed that never prices api-key turns eventually
    # trips the guard closed, instead of silently reading $0 forever.
    guard = BudgetGuard(BudgetGuardConfig(metering_dead_after=3))
    for _ in range(3):
        feed_budget_guard(
            guard,
            model="claude-opus-4-8",
            provider="anthropic",
            base_url="",
            api_key="",
            usage=CanonicalUsage(input_tokens=1000),
            cost_result=SimpleNamespace(status="estimated", amount_usd=None),
        )
    assert guard.before_call("terminal").code == "budget_metering_dead"


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
