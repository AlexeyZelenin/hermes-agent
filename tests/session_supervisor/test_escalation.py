"""EscalationRouter: routing, idempotency, dedup, retries, delivery-status recording."""

import pytest

from session_supervisor.escalation import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    EscalationRouter,
)
from session_supervisor.events import build_event
from tests.session_supervisor.util import T0, mins

SUBJECT = {
    "board": "ra",
    "task_id": "t_1",
    "task_title": "Test task",
    "session_id": "s1",
    "worker_id": "host:100",
    "attempt": 1,
    "log_ref": "/logs/r1.jsonl",
}


def make_event(kind: str = "incident_opened", now: float = T0, **overrides) -> dict:
    event = build_event(
        kind,
        now,
        overrides.pop("incident_key", "ra:t_1:stalled"),
        overrides.pop("anomaly_type", "stalled"),
        overrides.pop("severity", "critical"),
        overrides.pop("anomaly_since", now - mins(10)),
        SUBJECT,
        overrides.pop("metrics", {"heartbeat_silent_min": 10.0, "tokens_silent_min": 10.0}),
    )
    event.update(overrides)
    return event


class FakeTasks:
    """Task port recording calls; fails the next `fail_next` calls of each op."""

    def __init__(self):
        self.created = []
        self.comments = []
        self.reopened = []
        self.resolved = []
        self.fail_next = 0
        self._seq = 0

    def _maybe_fail(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("db locked")

    def create_incident(self, title, body, idempotency_key):
        self._maybe_fail()
        self._seq += 1
        self.created.append({"title": title, "body": body, "idempotency_key": idempotency_key})
        return f"t_inc{self._seq}"

    def add_comment(self, task_id, body):
        self._maybe_fail()
        self.comments.append((task_id, body))

    def reopen_incident(self, task_id, comment):
        self._maybe_fail()
        self.reopened.append((task_id, comment))

    def resolve_incident(self, task_id, comment):
        self._maybe_fail()
        self.resolved.append((task_id, comment))


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.fail_next = 0

    def send(self, text):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TimeoutError("telegram timeout")
        self.sent.append(text)


@pytest.fixture()
def tasks():
    return FakeTasks()


@pytest.fixture()
def notifier():
    return FakeNotifier()


def make_router(tasks, notifier, state_path=None, **kwargs):
    kwargs.setdefault("badge", "⚡")
    return EscalationRouter(tasks, notifier, state_path=state_path, **kwargs)


def test_opened_creates_one_card_and_one_push(tasks, notifier):
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    assert len(tasks.created) == 1
    assert len(notifier.sent) == 1
    card = tasks.created[0]
    assert card["title"] == "[incident] stalled: Test task (t_1)"
    for fragment in ("ra:t_1:stalled", "t_1", "host:100", "/logs/r1.jsonl", "s1"):
        assert fragment in card["body"]
    assert "⚡ ra" in notifier.sent[0]
    assert all(d.status == STATUS_SENT for d in router.outbox)


def test_duplicate_events_are_ignored(tasks, notifier):
    router = make_router(tasks, notifier)
    events = [make_event()]
    router.route(T0, events)
    router.route(T0 + 60, events)
    assert len(tasks.created) == 1
    assert len(notifier.sent) == 1


def test_second_open_for_live_card_becomes_comment(tasks, notifier):
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    router.route(T0 + 60, [make_event(now=T0 + 60)])
    assert len(tasks.created) == 1
    assert len(notifier.sent) == 1
    assert len(tasks.comments) == 1
    assert tasks.comments[0][0] == "t_inc1"


def test_comment_event_routes_to_card_without_push(tasks, notifier):
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    router.route(T0 + 60, [make_event("incident_comment", now=T0 + 60)])
    assert len(tasks.comments) == 1
    assert len(notifier.sent) == 1


def test_reminder_comments_and_pushes_with_mark(tasks, notifier):
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    reminder = make_event("incident_reminder", now=T0 + mins(30))
    reminder["reminder"] = "1/3"
    router.route(T0 + mins(30), [reminder])
    assert len(tasks.comments) == 1
    assert len(notifier.sent) == 2
    assert "напоминание 1/3" in notifier.sent[1]


def test_resolve_closes_card_without_push_then_reopen_pushes(tasks, notifier):
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    router.route(T0 + mins(5), [make_event("incident_resolved", now=T0 + mins(5))])
    assert len(tasks.resolved) == 1
    assert tasks.resolved[0][0] == "t_inc1"
    assert len(notifier.sent) == 1
    router.route(T0 + mins(10), [make_event("incident_reopened", now=T0 + mins(10))])
    assert tasks.reopened[0][0] == "t_inc1"
    assert len(tasks.created) == 1
    assert len(notifier.sent) == 2


def test_transient_create_failure_retries_with_backoff(tasks, notifier):
    tasks.fail_next = 2
    router = make_router(tasks, notifier, retry_base_s=60.0)
    router.route(T0, [make_event()])
    card = next(d for d in router.outbox if d.channel == "card")
    assert card.status == STATUS_PENDING
    assert card.attempts == 1
    assert "ConnectionError" in card.last_error
    assert card.next_attempt_at == T0 + 60
    router.flush(T0 + 30)  # before backoff: no attempt
    assert card.attempts == 1
    router.flush(T0 + 61)  # second failure, backoff doubles
    assert card.attempts == 2
    assert card.next_attempt_at == T0 + 61 + 120
    router.flush(T0 + 200)
    card = next(d for d in router.outbox if d.channel == "card")
    assert card.status == STATUS_SENT
    assert len(tasks.created) == 1


def test_comment_waits_for_open_and_lands_in_order(tasks, notifier):
    tasks.fail_next = 1
    router = make_router(tasks, notifier, retry_base_s=60.0)
    router.route(T0, [make_event()])
    router.route(T0 + 30, [make_event("incident_comment", now=T0 + 30)])
    assert tasks.comments == []  # blocked behind the pending create
    router.flush(T0 + 61)  # create retried and lands; the comment follows in order
    assert len(tasks.created) == 1
    assert tasks.comments[0][0] == "t_inc1"


def test_push_failure_is_independent_of_card(tasks, notifier):
    notifier.fail_next = 1
    router = make_router(tasks, notifier, retry_base_s=60.0)
    router.route(T0, [make_event()])
    assert len(tasks.created) == 1
    push = next(d for d in router.outbox if d.channel == "push")
    assert push.status == STATUS_PENDING
    router.flush(T0 + 61)
    push = next(d for d in router.outbox if d.channel == "push")
    assert push.status == STATUS_SENT
    assert len(notifier.sent) == 1


def test_permanent_failure_marks_failed_and_keeps_error(tasks, notifier):
    notifier.fail_next = 99
    router = make_router(tasks, notifier, max_attempts=3, retry_base_s=0.0)
    router.route(T0, [make_event()])
    router.flush(T0 + 1)
    router.flush(T0 + 2)
    push = next(d for d in router.outbox if d.channel == "push")
    assert push.status == STATUS_FAILED
    assert push.attempts == 3
    assert "TimeoutError" in push.last_error


def test_state_survives_restart(tasks, notifier, tmp_path):
    state = tmp_path / "escalation-state.json"
    notifier.fail_next = 1
    router = make_router(tasks, notifier, state_path=state)
    events = [make_event()]
    router.route(T0, events)
    assert len(notifier.sent) == 0

    restarted = make_router(tasks, notifier, state_path=state)
    restarted.route(T0 + mins(2), events)  # duplicate delivery after restart
    assert len(tasks.created) == 1  # no second card
    assert len(notifier.sent) == 1  # pending push delivered exactly once
    restarted.route(T0 + mins(3), [make_event("incident_comment", now=T0 + mins(3))])
    assert restarted.task_id_for("ra:t_1:stalled") == "t_inc1"
    assert tasks.comments[0][0] == "t_inc1"


def test_stats_counts_by_status(tasks, notifier):
    notifier.fail_next = 1
    router = make_router(tasks, notifier)
    router.route(T0, [make_event()])
    assert router.stats() == {STATUS_PENDING: 1, STATUS_SENT: 1, STATUS_FAILED: 0}
