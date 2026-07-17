"""End-to-end supervisor scenarios: tick pipeline, probing, dedup, persistence, dispatcher."""

import json

from tests.session_supervisor.util import (
    T0,
    CountingProber,
    alive_prober,
    dead_prober,
    looping_calls,
    make_run,
    mins,
    stalled_dispatcher,
    stalled_run,
)

from session_supervisor import DispatcherSnapshot, Supervisor, SupervisorConfig

CONTRACT_FIELDS = {
    "kind", "event_id", "incident_key", "anomaly_type", "severity", "detected_at",
    "anomaly_since", "metrics", "actions_taken", "board", "task_id", "task_title",
    "session_id", "worker_id", "attempt", "log_ref",
}


def decisions_of(sup: Supervisor, decision: str) -> list[dict]:
    return [d for d in sup.decisions if d["decision"] == decision]


class TestStalledEndToEnd:
    def test_confirmed_stall_emits_contract_compliant_incident(self):
        sup = Supervisor(prober=dead_prober)
        assert sup.tick(T0, [stalled_run(T0)]) == []
        assert len(decisions_of(sup, "suspected")) == 1

        events = sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        assert len(events) == 1
        event = events[0]
        assert CONTRACT_FIELDS <= set(event)
        assert event["kind"] == "incident_opened"
        assert event["incident_key"] == "ra:t_1:stalled"
        assert event["anomaly_type"] == "stalled"
        assert event["severity"] == "critical"
        assert event["task_title"] == "Test task"
        assert event["worker_id"] == "host:100"
        assert event["actions_taken"] == [{"action": "none"}]
        assert len(event["event_id"]) == 26
        assert event["anomaly_since"] < event["detected_at"]

    def test_alive_probe_is_false_positive_with_reprobe_cooldown(self):
        prober = CountingProber(alive=True)
        sup = Supervisor(prober=prober)
        for minute in range(3):
            now = T0 + mins(minute)
            assert sup.tick(now, [stalled_run(now)]) == []
        assert prober.calls == 1
        assert len(decisions_of(sup, "probe_alive_false_positive")) == 1
        assert sup.open_incidents == {}

    def test_startup_grace_suppresses_everything(self):
        sup = Supervisor(prober=dead_prober)
        run = make_run(
            T0, started_at=T0 - mins(1), last_heartbeat_at=None, last_token_usage_at=None
        )
        assert sup.tick(T0, [run]) == []
        assert sup.decisions == []

    def test_confirmed_incident_auto_resolves_when_run_recovers(self):
        sup = Supervisor(prober=dead_prober)
        sup.tick(T0, [stalled_run(T0)])
        sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        events = sup.tick(T0 + mins(2), [make_run(T0 + mins(2))])
        assert [e["kind"] for e in events] == ["incident_resolved"]
        assert sup.open_incidents == {}

    def test_restall_within_reopen_window_reopens_incident(self):
        sup = Supervisor(prober=dead_prober)
        sup.tick(T0, [stalled_run(T0)])
        sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        sup.tick(T0 + mins(2), [make_run(T0 + mins(2))])
        sup.tick(T0 + mins(10), [stalled_run(T0 + mins(10))])
        events = sup.tick(T0 + mins(11), [stalled_run(T0 + mins(11))])
        assert [e["kind"] for e in events] == ["incident_reopened"]


class TestLoopEndToEnd:
    def test_repeated_tool_calls_confirm_loop_incident(self):
        sup = Supervisor(prober=dead_prober)
        calls = looping_calls(12)
        sup.tick(T0, [make_run(T0, recent_tool_calls=calls)])
        events = sup.tick(T0 + mins(1), [make_run(T0 + mins(1), recent_tool_calls=calls)])
        assert [e["anomaly_type"] for e in events] == ["loop"]
        assert events[0]["kind"] == "incident_opened"
        assert events[0]["metrics"]["repeated_tool_calls"] == 12

    def test_stale_workspace_with_growing_tokens_confirms_loop(self):
        sup = Supervisor(prober=dead_prober)
        changed_at = T0 - mins(5)

        def run_at(now, tokens):
            return make_run(now, tokens_total=tokens, workspace_changed_at=changed_at)

        assert sup.tick(T0, [run_at(T0, 10_000)]) == []
        assert sup.tick(T0 + mins(16), [run_at(T0 + mins(16), 90_000)]) == []
        events = sup.tick(T0 + mins(17), [run_at(T0 + mins(17), 95_000)])
        assert [e["anomaly_type"] for e in events] == ["loop"]

    def test_workspace_change_resets_stale_anchor(self):
        sup = Supervisor(prober=dead_prober)
        sup.tick(T0, [make_run(T0, tokens_total=10_000, workspace_changed_at=T0 - mins(5))])
        run = make_run(
            T0 + mins(16), tokens_total=90_000, workspace_changed_at=T0 + mins(15)
        )
        assert sup.tick(T0 + mins(16), [run]) == []
        assert decisions_of(sup, "suspected") == []


class TestTokenOverspendEndToEnd:
    def test_overspend_confirms_without_probe(self):
        prober = CountingProber(alive=True)
        sup = Supervisor(prober=prober)
        run_over = lambda now: make_run(now, tokens_total=250_000, token_budget=100_000)
        assert sup.tick(T0, [run_over(T0)]) == []
        events = sup.tick(T0 + mins(1), [run_over(T0 + mins(1))])
        assert [e["anomaly_type"] for e in events] == ["token_overspend"]
        assert events[0]["severity"] == "critical"
        assert prober.calls == 0

    def test_warning_band_logs_but_does_not_escalate(self):
        sup = Supervisor(prober=dead_prober)
        run_warn = lambda now: make_run(now, tokens_total=150_000, token_budget=100_000)
        assert sup.tick(T0, [run_warn(T0)]) == []
        assert sup.tick(T0 + mins(1), [run_warn(T0 + mins(1))]) == []
        warnings = decisions_of(sup, "warning")
        assert {w["rule"] for w in warnings} == {"token_overspend"}
        assert sup.open_incidents == {}


class TestDispatcher:
    def test_three_stalled_ticks_open_critical_incident_without_probe(self):
        prober = CountingProber(alive=True)
        sup = Supervisor(prober=prober)
        assert sup.tick(T0, [], stalled_dispatcher()) == []
        assert sup.tick(T0 + mins(1), [], stalled_dispatcher()) == []
        events = sup.tick(T0 + mins(2), [], stalled_dispatcher())
        assert [e["kind"] for e in events] == ["incident_opened"]
        assert events[0]["anomaly_type"] == "dispatcher_stall"
        assert events[0]["severity"] == "critical"
        assert prober.calls == 0

    def test_exhausted_agent_limit_is_not_an_incident(self):
        sup = Supervisor()
        busy = DispatcherSnapshot(board="ra", ready_queue_size=4, spawns_last_tick=0, free_slots=0)
        for minute in range(5):
            assert sup.tick(T0 + mins(minute), [], busy) == []

    def test_dispatcher_recovery_resolves_incident(self):
        sup = Supervisor()
        for minute in range(3):
            sup.tick(T0 + mins(minute), [], stalled_dispatcher())
        healthy = DispatcherSnapshot(
            board="ra", ready_queue_size=2, spawns_last_tick=1, free_slots=2
        )
        events = sup.tick(T0 + mins(3), [], healthy)
        assert [e["kind"] for e in events] == ["incident_resolved"]


class TestPersistenceAndLogging:
    def test_state_survives_restart_without_duplicate_escalation(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        sup = Supervisor(prober=dead_prober, state_path=state_path)
        sup.tick(T0, [stalled_run(T0)])
        events = sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        assert [e["kind"] for e in events] == ["incident_opened"]

        restarted = Supervisor(prober=dead_prober, state_path=state_path)
        assert list(restarted.open_incidents) == ["ra:t_1:stalled"]
        events = restarted.tick(T0 + mins(2), [stalled_run(T0 + mins(2))])
        assert [e["kind"] for e in events] == []

    def test_decision_log_is_written_as_jsonl(self, tmp_path):
        log_path = tmp_path / "decisions.jsonl"
        sup = Supervisor(prober=dead_prober, decision_log_path=str(log_path))
        sup.tick(T0, [stalled_run(T0)])
        sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert entries == sup.decisions
        assert {"suspected", "probe_failed", "confirmed"} <= {e["decision"] for e in entries}

    def test_reminders_fire_from_supervisor_tick(self):
        sup = Supervisor(prober=dead_prober)
        sup.tick(T0, [stalled_run(T0)])
        sup.tick(T0 + mins(1), [stalled_run(T0 + mins(1))])
        events = sup.tick(T0 + mins(31), [stalled_run(T0 + mins(31))])
        assert [e["kind"] for e in events] == ["incident_reminder"]
        assert events[0]["reminder"] == "1/3"


def test_config_from_dict_ignores_unknown_keys():
    cfg = SupervisorConfig.from_dict(
        {"no_heartbeat_min": 7, "loop_poll_allowlist": ["poll_ci"], "unknown_key": 1}
    )
    assert cfg.no_heartbeat_min == 7
    assert cfg.loop_poll_allowlist == ("poll_ci",)
    assert cfg.token_hard_cap == 5_000_000
