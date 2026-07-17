"""Pure anomaly-rule checks (spec R1-R5). Each returns a Signal or None; no side effects."""

from dataclasses import dataclass, field

from .config import SupervisorConfig
from .snapshots import RunSnapshot

ANOMALY_STALLED = "stalled"
ANOMALY_LOOP = "loop"
ANOMALY_TOKEN_OVERSPEND = "token_overspend"
ANOMALY_DISPATCHER_STALL = "dispatcher_stall"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass(frozen=True)
class Signal:
    anomaly_type: str
    severity: str
    needs_probe: bool
    since: float
    metrics: dict = field(default_factory=dict)


def _last_activity(ts: float | None, run: RunSnapshot) -> float:
    return ts if ts is not None else run.started_at


def _in_startup_grace(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> bool:
    return now - run.started_at < cfg.startup_grace_min * 60


def heartbeat_silent_since(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> float | None:
    """R1: returns the timestamp heartbeats went silent, or None if healthy."""
    if _in_startup_grace(run, cfg, now):
        return None
    last = _last_activity(run.last_heartbeat_at, run)
    return last if now - last > cfg.no_heartbeat_min * 60 else None


def tokens_silent_since(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> float | None:
    """R2: returns the timestamp token usage stopped, or None if healthy."""
    if _in_startup_grace(run, cfg, now):
        return None
    last = _last_activity(run.last_token_usage_at, run)
    return last if now - last > cfg.no_tokens_min * 60 else None


def check_stalled(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> Signal | None:
    """R3: stalled signature = R1 AND R2 AND status=running."""
    if run.status != "running":
        return None
    hb_since = heartbeat_silent_since(run, cfg, now)
    tok_since = tokens_silent_since(run, cfg, now)
    if hb_since is None or tok_since is None:
        return None
    since = max(hb_since, tok_since)
    return Signal(
        anomaly_type=ANOMALY_STALLED,
        severity=SEVERITY_CRITICAL,
        needs_probe=True,
        since=since,
        metrics={
            "heartbeat_silent_min": round((now - hb_since) / 60, 1),
            "tokens_silent_min": round((now - tok_since) / 60, 1),
        },
    )


def _trailing_repeats(run: RunSnapshot, cfg: SupervisorConfig) -> int:
    calls = run.recent_tool_calls
    if not calls or calls[-1].tool in cfg.loop_poll_allowlist:
        return 0
    tail = (calls[-1].tool, calls[-1].args_fingerprint)
    count = 0
    for call in reversed(calls):
        if (call.tool, call.args_fingerprint) != tail:
            break
        count += 1
    return count


def check_loop_repeats(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> Signal | None:
    """R4a: >= N consecutive identical tool calls (allowlisted polling tools exempt)."""
    repeats = _trailing_repeats(run, cfg)
    if repeats < cfg.loop_repeat_threshold:
        return None
    return Signal(
        anomaly_type=ANOMALY_LOOP,
        severity=SEVERITY_CRITICAL,
        needs_probe=True,
        since=now,
        metrics={"repeated_tool_calls": repeats, "tool": run.recent_tool_calls[-1].tool},
    )


def check_loop_stale(
    run: RunSnapshot, cfg: SupervisorConfig, now: float, anchor: dict
) -> Signal | None:
    """R4b: tokens keep growing while the workspace is unchanged for >= loop_stale_min.

    `anchor` is supervisor-held state: {"at", "tokens", "workspace_changed_at"} captured
    when the workspace last changed.
    """
    if now - anchor["at"] < cfg.loop_stale_min * 60:
        return None
    if run.tokens_total <= anchor["tokens"]:
        return None
    return Signal(
        anomaly_type=ANOMALY_LOOP,
        severity=SEVERITY_CRITICAL,
        needs_probe=True,
        since=anchor["at"],
        metrics={
            "workspace_stale_min": round((now - anchor["at"]) / 60, 1),
            "tokens_grown": run.tokens_total - anchor["tokens"],
        },
    )


def check_token_overspend(run: RunSnapshot, cfg: SupervisorConfig, now: float) -> Signal | None:
    """R5: critical > budget x multiplier (or > hard cap); warning in 1x..2x budget band.

    The hard cap applies as a backstop even when a budget is set. No probe: the
    signal is a hard metric a liveness ping cannot refute.
    """
    metrics = {"tokens_total": run.tokens_total, "token_budget": run.token_budget}
    if run.tokens_total > cfg.token_hard_cap:
        metrics["token_hard_cap"] = cfg.token_hard_cap
        return Signal(ANOMALY_TOKEN_OVERSPEND, SEVERITY_CRITICAL, False, now, metrics)
    if run.token_budget is None:
        return None
    if run.tokens_total > run.token_budget * cfg.token_budget_multiplier:
        return Signal(ANOMALY_TOKEN_OVERSPEND, SEVERITY_CRITICAL, False, now, metrics)
    if run.tokens_total > run.token_budget:
        return Signal(ANOMALY_TOKEN_OVERSPEND, SEVERITY_WARNING, False, now, metrics)
    return None
