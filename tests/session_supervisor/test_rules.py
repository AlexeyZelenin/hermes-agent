"""Unit tests for the pure rule checks (R1-R5)."""

from tests.session_supervisor.util import T0, looping_calls, make_run, mins, stalled_run

from session_supervisor import SupervisorConfig
from session_supervisor.rules import (
    ANOMALY_LOOP,
    ANOMALY_STALLED,
    ANOMALY_TOKEN_OVERSPEND,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    check_loop_repeats,
    check_loop_stale,
    check_stalled,
    check_token_overspend,
    heartbeat_silent_since,
    tokens_silent_since,
)

CFG = SupervisorConfig()


class TestHeartbeatAndTokenSilence:
    def test_fresh_heartbeat_is_healthy(self):
        assert heartbeat_silent_since(make_run(T0), CFG, T0) is None

    def test_silent_heartbeat_detected(self):
        run = make_run(T0, last_heartbeat_at=T0 - mins(6))
        assert heartbeat_silent_since(run, CFG, T0) == T0 - mins(6)

    def test_no_heartbeat_ever_falls_back_to_started_at(self):
        run = make_run(T0, last_heartbeat_at=None, started_at=T0 - mins(7))
        assert heartbeat_silent_since(run, CFG, T0) == T0 - mins(7)

    def test_startup_grace_suppresses_signal(self):
        run = make_run(T0, started_at=T0 - mins(1), last_heartbeat_at=None)
        assert heartbeat_silent_since(run, CFG, T0) is None

    def test_token_silence_detected(self):
        run = make_run(T0, last_token_usage_at=T0 - mins(6))
        assert tokens_silent_since(run, CFG, T0) == T0 - mins(6)


class TestStalled:
    def test_both_silent_fires_stalled(self):
        signal = check_stalled(stalled_run(T0), CFG, T0)
        assert signal is not None
        assert signal.anomaly_type == ANOMALY_STALLED
        assert signal.severity == SEVERITY_CRITICAL
        assert signal.needs_probe is True
        assert signal.since == T0 - mins(10)

    def test_heartbeat_alone_does_not_fire(self):
        run = make_run(T0, last_heartbeat_at=T0 - mins(10))
        assert check_stalled(run, CFG, T0) is None

    def test_tokens_alone_does_not_fire(self):
        run = make_run(T0, last_token_usage_at=T0 - mins(10))
        assert check_stalled(run, CFG, T0) is None

    def test_non_running_status_does_not_fire(self):
        assert check_stalled(stalled_run(T0, status="done"), CFG, T0) is None


class TestLoopRepeats:
    def test_threshold_repeats_fire(self):
        run = make_run(T0, recent_tool_calls=looping_calls(10))
        signal = check_loop_repeats(run, CFG, T0)
        assert signal is not None
        assert signal.anomaly_type == ANOMALY_LOOP
        assert signal.metrics["repeated_tool_calls"] == 10

    def test_below_threshold_does_not_fire(self):
        run = make_run(T0, recent_tool_calls=looping_calls(9))
        assert check_loop_repeats(run, CFG, T0) is None

    def test_varied_args_break_the_streak(self):
        calls = looping_calls(5, fingerprint="a") + looping_calls(9, fingerprint="b")
        run = make_run(T0, recent_tool_calls=calls)
        assert check_loop_repeats(run, CFG, T0) is None

    def test_allowlisted_polling_tool_is_exempt(self):
        cfg = SupervisorConfig(loop_poll_allowlist=("poll_ci",))
        run = make_run(T0, recent_tool_calls=looping_calls(20, tool="poll_ci"))
        assert check_loop_repeats(run, cfg, T0) is None


class TestLoopStale:
    def test_tokens_grow_with_stale_workspace_fires(self):
        anchor = {"at": T0 - mins(16), "tokens": 5_000, "workspace_changed_at": T0 - mins(30)}
        run = make_run(T0, tokens_total=50_000, workspace_changed_at=T0 - mins(30))
        signal = check_loop_stale(run, CFG, T0, anchor)
        assert signal is not None
        assert signal.anomaly_type == ANOMALY_LOOP
        assert signal.metrics["tokens_grown"] == 45_000

    def test_recent_anchor_does_not_fire(self):
        anchor = {"at": T0 - mins(5), "tokens": 5_000, "workspace_changed_at": T0 - mins(30)}
        run = make_run(T0, tokens_total=50_000, workspace_changed_at=T0 - mins(30))
        assert check_loop_stale(run, CFG, T0, anchor) is None

    def test_no_token_growth_does_not_fire(self):
        anchor = {"at": T0 - mins(20), "tokens": 10_000, "workspace_changed_at": T0 - mins(30)}
        run = make_run(T0, tokens_total=10_000, workspace_changed_at=T0 - mins(30))
        assert check_loop_stale(run, CFG, T0, anchor) is None


class TestTokenOverspend:
    def test_within_budget_is_healthy(self):
        run = make_run(T0, tokens_total=90_000, token_budget=100_000)
        assert check_token_overspend(run, CFG, T0) is None

    def test_between_1x_and_2x_is_warning(self):
        run = make_run(T0, tokens_total=150_000, token_budget=100_000)
        signal = check_token_overspend(run, CFG, T0)
        assert signal.severity == SEVERITY_WARNING
        assert signal.anomaly_type == ANOMALY_TOKEN_OVERSPEND

    def test_over_2x_budget_is_critical(self):
        run = make_run(T0, tokens_total=200_001, token_budget=100_000)
        signal = check_token_overspend(run, CFG, T0)
        assert signal.severity == SEVERITY_CRITICAL
        assert signal.needs_probe is False

    def test_no_budget_below_hard_cap_is_healthy(self):
        run = make_run(T0, tokens_total=4_999_999, token_budget=None)
        assert check_token_overspend(run, CFG, T0) is None

    def test_hard_cap_is_critical_even_without_budget(self):
        run = make_run(T0, tokens_total=5_000_001, token_budget=None)
        signal = check_token_overspend(run, CFG, T0)
        assert signal.severity == SEVERITY_CRITICAL
