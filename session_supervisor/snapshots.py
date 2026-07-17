"""Input snapshots the supervisor observes each tick. All timestamps are epoch seconds UTC."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args_fingerprint: str


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    board: str
    task_id: str
    task_title: str
    session_id: str
    worker_id: str
    attempt: int
    status: str
    started_at: float
    last_heartbeat_at: float | None
    last_token_usage_at: float | None
    tokens_total: int
    token_budget: int | None
    log_ref: str
    recent_tool_calls: tuple = field(default=())
    workspace_changed_at: float | None = None


@dataclass(frozen=True)
class DispatcherSnapshot:
    board: str
    ready_queue_size: int
    spawns_last_tick: int
    free_slots: int
    log_ref: str = ""


@dataclass(frozen=True)
class ProbeResult:
    alive: bool
    detail: str = ""
