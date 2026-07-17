"""Supervisor thresholds; defaults per spec, overridable from board.json `supervisor` block."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class SupervisorConfig:
    no_heartbeat_min: float = 5.0
    no_tokens_min: float = 5.0
    startup_grace_min: float = 2.0
    loop_repeat_threshold: int = 10
    loop_stale_min: float = 15.0
    loop_poll_allowlist: tuple = ()
    token_budget_multiplier: float = 2.0
    token_hard_cap: int = 5_000_000
    dispatcher_stall_ticks: int = 3
    reprobe_cooldown_min: float = 15.0
    reopen_window_min: float = 30.0
    reminder_first_min: float = 30.0
    reminder_repeat_min: float = 120.0
    reminder_max: int = 3

    @classmethod
    def from_dict(cls, raw: dict) -> "SupervisorConfig":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        if "loop_poll_allowlist" in kwargs:
            kwargs["loop_poll_allowlist"] = tuple(kwargs["loop_poll_allowlist"])
        return cls(**kwargs)
