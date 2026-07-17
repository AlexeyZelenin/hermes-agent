"""Wiring that binds the deterministic ``session_supervisor`` core to live hermes-agent data.

The core (``session_supervisor``) is pure: it observes ``RunSnapshot`` / ``DispatcherSnapshot``
objects and returns incident events. This package builds those snapshots from the real board
(``hermes_cli.kanban_db``) and the token ledger (``zeus.db``), supplies a real liveness prober,
and drives one supervision pass per dispatcher tick, routing events to a triage card + operator
push. It is the "gateway wiring + event hooks" layer requested by task t_cbf67d89.
"""

from .prober import ProcessLivenessProber
from .service import SupervisorService
from .sources import (
    build_dispatcher_snapshot,
    build_run_snapshots,
    count_running,
    token_usage_totals,
)

__all__ = [
    "ProcessLivenessProber",
    "SupervisorService",
    "build_dispatcher_snapshot",
    "build_run_snapshots",
    "count_running",
    "token_usage_totals",
]
