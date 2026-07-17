"""Incident lifecycle: dedup by incident_key, reopen window, reminders, resolution."""

from dataclasses import asdict, dataclass, field

from .config import SupervisorConfig
from .events import build_event


@dataclass
class Incident:
    incident_key: str
    anomaly_type: str
    severity: str
    anomaly_since: float
    opened_at: float
    subject: dict
    metrics: dict = field(default_factory=dict)
    resolved_at: float | None = None
    reminders_sent: int = 0
    last_reminder_at: float | None = None
    comment_count: int = 0


def incident_key(board: str, task_id: str, anomaly_type: str) -> str:
    return f"{board}:{task_id}:{anomaly_type}"


class IncidentManager:
    def __init__(self, cfg: SupervisorConfig):
        self._cfg = cfg
        self._open: dict[str, Incident] = {}
        self._closed: dict[str, Incident] = {}

    @property
    def open_incidents(self) -> dict:
        return dict(self._open)

    def open_key_for(self, key: str) -> Incident | None:
        return self._open.get(key)

    def escalate(
        self,
        now: float,
        key: str,
        anomaly_type: str,
        severity: str,
        anomaly_since: float,
        subject: dict,
        metrics: dict,
    ) -> dict:
        """Route a confirmed anomaly: new incident, comment on an open one, or reopen."""
        existing = self._open.get(key)
        if existing is not None:
            existing.comment_count += 1
            existing.metrics = dict(metrics)
            return self._event("incident_comment", now, existing)
        closed = self._closed.get(key)
        reopen_window = self._cfg.reopen_window_min * 60
        if closed is not None and now - closed.resolved_at <= reopen_window:
            closed.resolved_at = None
            closed.metrics = dict(metrics)
            self._open[key] = self._closed.pop(key)
            return self._event("incident_reopened", now, closed)
        incident = Incident(key, anomaly_type, severity, anomaly_since, now, subject, dict(metrics))
        self._open[key] = incident
        return self._event("incident_opened", now, incident)

    def resolve(self, now: float, key: str) -> dict | None:
        incident = self._open.pop(key, None)
        if incident is None:
            return None
        incident.resolved_at = now
        self._closed[key] = incident
        self._prune_closed(now)
        return self._event("incident_resolved", now, incident)

    def due_reminders(self, now: float) -> list[dict]:
        events = []
        for incident in self._open.values():
            if incident.reminders_sent >= self._cfg.reminder_max:
                continue
            if incident.reminders_sent == 0:
                due = incident.opened_at + self._cfg.reminder_first_min * 60
            else:
                due = incident.last_reminder_at + self._cfg.reminder_repeat_min * 60
            if now < due:
                continue
            incident.reminders_sent += 1
            incident.last_reminder_at = now
            event = self._event("incident_reminder", now, incident)
            event["reminder"] = f"{incident.reminders_sent}/{self._cfg.reminder_max}"
            events.append(event)
        return events

    def _event(self, kind: str, now: float, incident: Incident) -> dict:
        return build_event(
            kind,
            now,
            incident.incident_key,
            incident.anomaly_type,
            incident.severity,
            incident.anomaly_since,
            incident.subject,
            incident.metrics,
        )

    def _prune_closed(self, now: float) -> None:
        window = self._cfg.reopen_window_min * 60
        stale = [k for k, v in self._closed.items() if now - v.resolved_at > window]
        for key in stale:
            del self._closed[key]

    def to_dict(self) -> dict:
        return {
            "open": {k: asdict(v) for k, v in self._open.items()},
            "closed": {k: asdict(v) for k, v in self._closed.items()},
        }

    def restore(self, raw: dict) -> None:
        self._open = {k: Incident(**v) for k, v in raw.get("open", {}).items()}
        self._closed = {k: Incident(**v) for k, v in raw.get("closed", {}).items()}
