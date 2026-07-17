"""Escalation delivery: supervisor events -> triage incident card + operator push.

Delivery side of contract t_adf76f16 (§1, §3-§5): one open incident is exactly one
triage card and one push; repeats become card comments; reminders push again;
resolution closes the card without a push. Delivery is at-least-once through a
persistent outbox: every send is recorded with a status, transient channel errors
are retried with exponential backoff, and duplicate event delivery is a no-op
(dedup by event_id, plus the card idempotency_key guards create at the DB level).

Ports (injected, any raised exception is treated as a transient delivery error):
  tasks:    create_incident(title, body, idempotency_key) -> task_id
            add_comment(task_id, body)
            reopen_incident(task_id, comment)
            resolve_incident(task_id, comment)
  notifier: send(text)
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .rendering import (
    render_card_body,
    render_card_title,
    render_comment,
    render_push,
    render_resolve_comment,
)

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

CHANNEL_CARD = "card"
CHANNEL_PUSH = "push"

OP_OPEN = "open"
OP_COMMENT = "comment"
OP_REOPEN = "reopen"
OP_RESOLVE = "resolve"
OP_PUSH = "push"

_PROCESSED_CAP = 1000
_TERMINAL_KEEP = 200


class MissingIncidentCardError(RuntimeError):
    """Card operation for an incident whose card was never successfully created."""


@dataclass
class Delivery:
    delivery_id: str
    event_id: str
    incident_key: str
    channel: str
    op: str
    payload: dict
    status: str = STATUS_PENDING
    attempts: int = 0
    last_error: str | None = None
    next_attempt_at: float = 0.0
    sent_at: float | None = None


class EscalationRouter:
    def __init__(
        self,
        tasks,
        notifier,
        state_path: str | None = None,
        badge: str = "",
        max_attempts: int = 5,
        retry_base_s: float = 60.0,
    ):
        self._tasks = tasks
        self._notifier = notifier
        self._state_path = Path(state_path) if state_path else None
        self._badge = badge
        self.max_attempts = max_attempts
        self.retry_base_s = retry_base_s
        self._incidents: dict[str, dict] = {}
        self._processed: list[str] = []
        self._outbox: list[Delivery] = []
        self._load_state()

    def route(self, now: float, events: list[dict]) -> None:
        """Enqueue deliveries for new events and attempt everything due."""
        for event in events:
            self._enqueue(event)
        self.flush(now)

    def flush(self, now: float) -> None:
        """Attempt every pending delivery that is due and not blocked by an earlier one."""
        for delivery in self._outbox:
            if delivery.status != STATUS_PENDING or now < delivery.next_attempt_at:
                continue
            if self._blocked(delivery):
                continue
            self._attempt(now, delivery)
        self._save_state()

    @property
    def outbox(self) -> list[Delivery]:
        return list(self._outbox)

    def task_id_for(self, incident_key: str) -> str | None:
        incident = self._incidents.get(incident_key)
        return incident["task_id"] if incident else None

    def stats(self) -> dict:
        counts = {STATUS_PENDING: 0, STATUS_SENT: 0, STATUS_FAILED: 0}
        for delivery in self._outbox:
            counts[delivery.status] += 1
        return counts

    # -- enqueue -----------------------------------------------------------

    def _enqueue(self, event: dict) -> None:
        if event["event_id"] in self._processed:
            return
        self._processed.append(event["event_id"])
        del self._processed[:-_PROCESSED_CAP]
        for channel, op in self._plan(event):
            self._outbox.append(
                Delivery(
                    delivery_id=f"{event['event_id']}:{channel}:{op}",
                    event_id=event["event_id"],
                    incident_key=event["incident_key"],
                    channel=channel,
                    op=op,
                    payload=self._payload(event, channel, op),
                )
            )

    def _plan(self, event: dict) -> list[tuple]:
        kind = event["kind"]
        known = self._incidents.get(event["incident_key"])
        card_open = known is not None and not known.get("resolved", False)
        if kind == "incident_opened":
            if card_open:
                # The detector re-sent an open for a live card (e.g. it lost its
                # state): keep the one-task-per-anomaly invariant, comment instead.
                return [(CHANNEL_CARD, OP_COMMENT)]
            return [(CHANNEL_CARD, OP_OPEN), (CHANNEL_PUSH, OP_PUSH)]
        if kind == "incident_reopened":
            card_op = OP_OPEN if known is None else OP_REOPEN
            return [(CHANNEL_CARD, card_op), (CHANNEL_PUSH, OP_PUSH)]
        if kind == "incident_comment":
            return [(CHANNEL_CARD, OP_COMMENT)]
        if kind == "incident_reminder":
            return [(CHANNEL_CARD, OP_COMMENT), (CHANNEL_PUSH, OP_PUSH)]
        if kind == "incident_resolved":
            return [(CHANNEL_CARD, OP_RESOLVE)]
        raise ValueError(f"unknown event kind: {kind}")

    def _payload(self, event: dict, channel: str, op: str) -> dict:
        if channel == CHANNEL_PUSH:
            return {"text": render_push(event, self._badge)}
        if op == OP_OPEN:
            return {
                "title": render_card_title(event),
                "body": render_card_body(event),
                "idempotency_key": f"incident:{event['incident_key']}:{event['event_id']}",
            }
        if op == OP_RESOLVE:
            return {"body": render_resolve_comment(event)}
        return {"body": render_comment(event)}

    # -- delivery ----------------------------------------------------------

    def _blocked(self, delivery: Delivery) -> bool:
        """Card ops for one incident must land in enqueue order (a comment cannot
        precede the create that yields its task_id); pushes follow the same FIFO."""
        for other in self._outbox:
            if other is delivery:
                return False
            if (
                other.incident_key == delivery.incident_key
                and other.channel == delivery.channel
                and other.status == STATUS_PENDING
            ):
                return True
        return False

    def _attempt(self, now: float, delivery: Delivery) -> None:
        delivery.attempts += 1
        try:
            self._dispatch(delivery)
        except Exception as exc:
            delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if delivery.attempts >= self.max_attempts:
                delivery.status = STATUS_FAILED
            else:
                backoff = self.retry_base_s * (2 ** (delivery.attempts - 1))
                delivery.next_attempt_at = now + backoff
            return
        delivery.status = STATUS_SENT
        delivery.sent_at = now
        delivery.last_error = None

    def _dispatch(self, delivery: Delivery) -> None:
        if delivery.channel == CHANNEL_PUSH:
            self._notifier.send(delivery.payload["text"])
            return
        if delivery.op == OP_OPEN:
            task_id = self._tasks.create_incident(
                delivery.payload["title"],
                delivery.payload["body"],
                delivery.payload["idempotency_key"],
            )
            self._incidents[delivery.incident_key] = {"task_id": task_id, "resolved": False}
            return
        incident = self._incidents.get(delivery.incident_key)
        if incident is None:
            raise MissingIncidentCardError(delivery.incident_key)
        task_id = incident["task_id"]
        if delivery.op == OP_COMMENT:
            self._tasks.add_comment(task_id, delivery.payload["body"])
        elif delivery.op == OP_REOPEN:
            self._tasks.reopen_incident(task_id, delivery.payload["body"])
            incident["resolved"] = False
        elif delivery.op == OP_RESOLVE:
            self._tasks.resolve_incident(task_id, delivery.payload["body"])
            incident["resolved"] = True

    # -- persistence ---------------------------------------------------------

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        pending = [d for d in self._outbox if d.status == STATUS_PENDING]
        terminal = [d for d in self._outbox if d.status != STATUS_PENDING]
        self._outbox = terminal[-_TERMINAL_KEEP:] + pending
        state = {
            "incidents": self._incidents,
            "processed_events": self._processed,
            "outbox": [asdict(d) for d in self._outbox],
        }
        self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._incidents = raw.get("incidents", {})
        self._processed = raw.get("processed_events", [])
        self._outbox = [Delivery(**d) for d in raw.get("outbox", [])]
