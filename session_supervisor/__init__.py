"""Session-monitoring supervisor: detects worker anomalies and emits incident events.

Implements the operator-approved spec from tasks t_935565c0 (rules R1-R6) and
t_adf76f16 (escalation event contract).
"""

from .config import SupervisorConfig
from .escalation import EscalationRouter
from .snapshots import DispatcherSnapshot, ProbeResult, RunSnapshot, ToolCall
from .supervisor import Supervisor

__all__ = [
    "DispatcherSnapshot",
    "EscalationRouter",
    "ProbeResult",
    "RunSnapshot",
    "Supervisor",
    "SupervisorConfig",
    "ToolCall",
]
