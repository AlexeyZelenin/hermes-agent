"""Deterministic supervisor tick: evaluate rules, drive the incident state machine.

Per-anomaly state machine (spec t_935565c0, section 3):
  healthy -> suspected (signal fires; recorded, no event)
          -> probed on the next tick (liveness ping via injected prober)
          -> confirmed (probe failed / no probe needed) => escalation event
          -> back to healthy when the signal clears (open incident auto-resolves).
Probe-alive marks a false positive and suppresses re-probing for reprobe_cooldown_min.
Dispatcher stall (R6) skips the probe step and escalates directly.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import rules
from .config import SupervisorConfig
from .events import contract_fields, iso_utc
from .incidents import IncidentManager, incident_key
from .snapshots import DispatcherSnapshot, ProbeResult, RunSnapshot

PHASE_SUSPECTED = "suspected"
PHASE_PROBED_ALIVE = "probed_alive"
PHASE_CONFIRMED = "confirmed"


@dataclass
class RuleState:
    phase: str
    since: float
    last_probe_at: float | None = None


class Supervisor:
    def __init__(
        self,
        config: SupervisorConfig | None = None,
        prober=None,
        state_path: str | None = None,
        decision_log_path: str | None = None,
    ):
        self.config = config or SupervisorConfig()
        self._prober = prober
        self._state_path = Path(state_path) if state_path else None
        self._decision_log_path = Path(decision_log_path) if decision_log_path else None
        self._incidents = IncidentManager(self.config)
        self._rule_states: dict[tuple[str, str], RuleState] = {}
        self._loop_anchors: dict[str, dict] = {}
        self._dispatcher_stalled_ticks = 0
        self.decisions: list[dict] = []
        self._load_state()

    @property
    def open_incidents(self) -> dict:
        return self._incidents.open_incidents

    def tick(
        self, now: float, runs: list[RunSnapshot], dispatcher: DispatcherSnapshot | None = None
    ) -> list[dict]:
        """One supervision pass; returns the incident events produced by this tick."""
        events: list[dict] = []
        for run in runs:
            if run.status == "running":
                events.extend(self._check_run(now, run))
        self._prune_unobserved(now, runs)
        if dispatcher is not None:
            events.extend(self._check_dispatcher(now, dispatcher))
        events.extend(self._incidents.due_reminders(now))
        self._save_state()
        return events

    # -- per-run pipeline -------------------------------------------------

    def _check_run(self, now: float, run: RunSnapshot) -> list[dict]:
        self._log_solo_warnings(now, run)
        events = []
        active: set[str] = set()
        for signal in self._candidate_signals(now, run):
            active.add(signal.anomaly_type)
            event = self._advance(now, run, signal)
            if event is not None:
                events.append(event)
        events.extend(self._clear_inactive(now, run, active))
        return events

    def _candidate_signals(self, now: float, run: RunSnapshot) -> list[rules.Signal]:
        signals = []
        stalled = rules.check_stalled(run, self.config, now)
        if stalled is not None:
            signals.append(stalled)
        loop = rules.check_loop_repeats(run, self.config, now) or rules.check_loop_stale(
            run, self.config, now, self._loop_anchor(now, run)
        )
        if loop is not None:
            signals.append(loop)
        overspend = rules.check_token_overspend(run, self.config, now)
        if overspend is not None and overspend.severity == rules.SEVERITY_CRITICAL:
            signals.append(overspend)
        return signals

    def _loop_anchor(self, now: float, run: RunSnapshot) -> dict:
        anchor = self._loop_anchors.get(run.run_id)
        if anchor is None or anchor["workspace_changed_at"] != run.workspace_changed_at:
            anchor = {
                "at": now,
                "tokens": run.tokens_total,
                "workspace_changed_at": run.workspace_changed_at,
            }
            self._loop_anchors[run.run_id] = anchor
        return anchor

    def _advance(self, now: float, run: RunSnapshot, signal: rules.Signal) -> dict | None:
        key = (run.run_id, signal.anomaly_type)
        state = self._rule_states.get(key)
        if state is None:
            self._rule_states[key] = RuleState(PHASE_SUSPECTED, signal.since)
            self._decide(now, run.run_id, signal.anomaly_type, "suspected", signal.metrics)
            return None
        if state.phase == PHASE_CONFIRMED:
            return None
        if state.phase == PHASE_PROBED_ALIVE:
            cooldown = self.config.reprobe_cooldown_min * 60
            if now - state.last_probe_at < cooldown:
                return None
            state.phase = PHASE_SUSPECTED
        return self._confirm_or_hold(now, run, signal, state)

    def _confirm_or_hold(
        self, now: float, run: RunSnapshot, signal: rules.Signal, state: RuleState
    ) -> dict | None:
        if signal.needs_probe:
            result = self._probe(run, signal.anomaly_type)
            if result.alive:
                state.phase = PHASE_PROBED_ALIVE
                state.last_probe_at = now
                self._decide(
                    now, run.run_id, signal.anomaly_type, "probe_alive_false_positive",
                    {"probe_detail": result.detail},
                )
                return None
            self._decide(
                now, run.run_id, signal.anomaly_type, "probe_failed",
                {"probe_detail": result.detail},
            )
        state.phase = PHASE_CONFIRMED
        self._decide(now, run.run_id, signal.anomaly_type, "confirmed", signal.metrics)
        return self._incidents.escalate(
            now,
            incident_key(run.board, run.task_id, signal.anomaly_type),
            signal.anomaly_type,
            signal.severity,
            state.since,
            contract_fields(run),
            signal.metrics,
        )

    def _probe(self, run: RunSnapshot, anomaly_type: str) -> ProbeResult:
        if self._prober is None:
            return ProbeResult(alive=False, detail="prober_unavailable")
        return self._prober(run, anomaly_type)

    def _clear_inactive(self, now: float, run: RunSnapshot, active: set) -> list[dict]:
        events = []
        for key in [k for k in self._rule_states if k[0] == run.run_id and k[1] not in active]:
            state = self._rule_states.pop(key)
            self._decide(now, run.run_id, key[1], "cleared", {"phase_was": state.phase})
            if state.phase == PHASE_CONFIRMED:
                resolved = self._incidents.resolve(
                    now, incident_key(run.board, run.task_id, key[1])
                )
                if resolved is not None:
                    events.append(resolved)
        return events

    def _log_solo_warnings(self, now: float, run: RunSnapshot) -> None:
        if rules.heartbeat_silent_since(run, self.config, now) is not None:
            self._decide(now, run.run_id, "heartbeat", "warning", {"rule": "R1"})
        if rules.tokens_silent_since(run, self.config, now) is not None:
            self._decide(now, run.run_id, "token_usage", "warning", {"rule": "R2"})
        overspend = rules.check_token_overspend(run, self.config, now)
        if overspend is not None and overspend.severity == rules.SEVERITY_WARNING:
            self._decide(now, run.run_id, overspend.anomaly_type, "warning", overspend.metrics)

    # -- dispatcher (R6) ---------------------------------------------------

    def _check_dispatcher(self, now: float, disp: DispatcherSnapshot) -> list[dict]:
        stalled = disp.ready_queue_size > 0 and disp.spawns_last_tick == 0 and disp.free_slots > 0
        key = incident_key(disp.board, "dispatcher", rules.ANOMALY_DISPATCHER_STALL)
        if not stalled:
            self._dispatcher_stalled_ticks = 0
            resolved = self._incidents.resolve(now, key)
            if resolved is not None:
                self._decide(now, "dispatcher", rules.ANOMALY_DISPATCHER_STALL, "cleared", {})
            return [resolved] if resolved is not None else []
        self._dispatcher_stalled_ticks += 1
        if self._dispatcher_stalled_ticks < self.config.dispatcher_stall_ticks:
            return []
        if self._incidents.open_key_for(key) is not None:
            return []
        metrics = {
            "ready_queue_size": disp.ready_queue_size,
            "stalled_ticks": self._dispatcher_stalled_ticks,
            "free_slots": disp.free_slots,
        }
        self._decide(now, "dispatcher", rules.ANOMALY_DISPATCHER_STALL, "confirmed", metrics)
        subject = {
            "board": disp.board,
            "task_id": "dispatcher",
            "task_title": "dispatcher",
            "session_id": "dispatcher",
            "worker_id": "dispatcher",
            "attempt": 0,
            "log_ref": disp.log_ref,
        }
        event = self._incidents.escalate(
            now, key, rules.ANOMALY_DISPATCHER_STALL, rules.SEVERITY_CRITICAL,
            now, subject, metrics,
        )
        return [event]

    # -- bookkeeping ---------------------------------------------------------

    def _prune_unobserved(self, now: float, runs: list[RunSnapshot]) -> None:
        """Drop rule state and loop anchors for runs no longer in the scan.

        Open incidents for vanished runs are intentionally kept: a human still
        needs to look at them; reminders keep firing up to reminder_max.
        """
        seen = {run.run_id for run in runs}
        for key in [k for k in self._rule_states if k[0] not in seen]:
            del self._rule_states[key]
        for run_id in [r for r in self._loop_anchors if r not in seen]:
            del self._loop_anchors[run_id]

    def _decide(self, now: float, scope: str, rule: str, decision: str, details: dict) -> None:
        entry = {
            "ts": iso_utc(now),
            "scope": scope,
            "rule": rule,
            "decision": decision,
            "details": details,
        }
        self.decisions.append(entry)
        if self._decision_log_path is not None:
            with self._decision_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        state = {
            "rule_states": {
                f"{run_id}\t{anomaly}": asdict(v)
                for (run_id, anomaly), v in self._rule_states.items()
            },
            "loop_anchors": self._loop_anchors,
            "dispatcher_stalled_ticks": self._dispatcher_stalled_ticks,
            "incidents": self._incidents.to_dict(),
        }
        self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._rule_states = {
            tuple(key.split("\t", 1)): RuleState(**value)
            for key, value in raw.get("rule_states", {}).items()
        }
        self._loop_anchors = raw.get("loop_anchors", {})
        self._dispatcher_stalled_ticks = raw.get("dispatcher_stalled_ticks", 0)
        self._incidents.restore(raw.get("incidents", {}))
