"""Shared test fixtures: a healthy run snapshot and time helpers."""

from session_supervisor import DispatcherSnapshot, ProbeResult, RunSnapshot, ToolCall

T0 = 1_700_000_000.0


def mins(n: float) -> float:
    return n * 60


def make_run(now: float = T0, **overrides) -> RunSnapshot:
    """A healthy running run: started an hour ago, heartbeat and tokens fresh."""
    base = dict(
        run_id="r1",
        board="ra",
        task_id="t_1",
        task_title="Test task",
        session_id="s1",
        worker_id="host:100",
        attempt=1,
        status="running",
        started_at=now - mins(60),
        last_heartbeat_at=now - 30,
        last_token_usage_at=now - 30,
        tokens_total=10_000,
        token_budget=None,
        log_ref="/logs/r1.jsonl",
    )
    base.update(overrides)
    return RunSnapshot(**base)


def stalled_run(now: float, **overrides) -> RunSnapshot:
    """A run silent on both heartbeat and tokens for 10 minutes."""
    return make_run(
        now,
        last_heartbeat_at=now - mins(10),
        last_token_usage_at=now - mins(10),
        **overrides,
    )


def looping_calls(n: int, tool: str = "Bash", fingerprint: str = "same-args") -> tuple:
    return tuple(ToolCall(tool, fingerprint) for _ in range(n))


def dead_prober(run, anomaly_type):
    return ProbeResult(alive=False, detail="probe timeout")


def alive_prober(run, anomaly_type):
    return ProbeResult(alive=True, detail="agent responded")


class CountingProber:
    def __init__(self, alive: bool):
        self.alive = alive
        self.calls = 0

    def __call__(self, run, anomaly_type):
        self.calls += 1
        return ProbeResult(alive=self.alive)


def stalled_dispatcher(board: str = "ra") -> DispatcherSnapshot:
    return DispatcherSnapshot(board=board, ready_queue_size=4, spawns_last_tick=0, free_slots=2)
