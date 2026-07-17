"""Incident event construction per the escalation contract (t_adf76f16, section 2)."""

import os
from datetime import datetime, timezone

from .snapshots import RunSnapshot

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

EVENT_KINDS = (
    "incident_opened",
    "incident_comment",
    "incident_reopened",
    "incident_reminder",
    "incident_resolved",
)


def new_ulid(now: float) -> str:
    ts_ms = int(now * 1000) & ((1 << 48) - 1)
    value = (ts_ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD32[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def contract_fields(run: RunSnapshot) -> dict:
    """Subject fields of the escalation event that come from the run snapshot."""
    return {
        "board": run.board,
        "task_id": run.task_id,
        "task_title": run.task_title,
        "session_id": run.session_id,
        "worker_id": run.worker_id,
        "attempt": run.attempt,
        "log_ref": run.log_ref,
    }


def build_event(
    kind: str,
    now: float,
    incident_key: str,
    anomaly_type: str,
    severity: str,
    anomaly_since: float,
    subject: dict,
    metrics: dict,
    actions_taken: list | None = None,
) -> dict:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    return {
        "kind": kind,
        "event_id": new_ulid(now),
        "incident_key": incident_key,
        "anomaly_type": anomaly_type,
        "severity": severity,
        "detected_at": iso_utc(now),
        "anomaly_since": iso_utc(anomaly_since),
        "metrics": dict(metrics),
        "actions_taken": actions_taken or [{"action": "none"}],
        **subject,
    }
