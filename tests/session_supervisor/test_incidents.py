"""Incident lifecycle tests: dedup, reopen window, reminders, resolution."""

from tests.session_supervisor.util import T0, mins

from session_supervisor import SupervisorConfig
from session_supervisor.incidents import IncidentManager, incident_key

SUBJECT = {
    "board": "ra",
    "task_id": "t_1",
    "task_title": "Test task",
    "session_id": "s1",
    "worker_id": "host:100",
    "attempt": 1,
    "log_ref": "/logs/r1.jsonl",
}
KEY = incident_key("ra", "t_1", "stalled")


def escalate(manager: IncidentManager, now: float) -> dict:
    return manager.escalate(now, KEY, "stalled", "critical", now - mins(10), SUBJECT, {"m": 1})


def test_first_escalation_opens_incident():
    manager = IncidentManager(SupervisorConfig())
    event = escalate(manager, T0)
    assert event["kind"] == "incident_opened"
    assert event["incident_key"] == KEY
    assert list(manager.open_incidents) == [KEY]


def test_repeat_escalation_is_a_comment_not_a_new_incident():
    manager = IncidentManager(SupervisorConfig())
    escalate(manager, T0)
    event = escalate(manager, T0 + mins(1))
    assert event["kind"] == "incident_comment"
    assert len(manager.open_incidents) == 1


def test_reescalation_within_reopen_window_reopens_same_incident():
    manager = IncidentManager(SupervisorConfig())
    escalate(manager, T0)
    assert manager.resolve(T0 + mins(5), KEY)["kind"] == "incident_resolved"
    event = escalate(manager, T0 + mins(20))
    assert event["kind"] == "incident_reopened"
    assert list(manager.open_incidents) == [KEY]


def test_reescalation_after_reopen_window_opens_new_incident():
    manager = IncidentManager(SupervisorConfig())
    escalate(manager, T0)
    manager.resolve(T0 + mins(5), KEY)
    event = escalate(manager, T0 + mins(5) + mins(31))
    assert event["kind"] == "incident_opened"


def test_resolve_unknown_key_returns_none():
    manager = IncidentManager(SupervisorConfig())
    assert manager.resolve(T0, "ra:t_x:stalled") is None


def test_reminder_schedule_first_at_30min_then_every_2h_max_3():
    manager = IncidentManager(SupervisorConfig())
    escalate(manager, T0)
    assert manager.due_reminders(T0 + mins(29)) == []
    first = manager.due_reminders(T0 + mins(30))
    assert len(first) == 1
    assert first[0]["kind"] == "incident_reminder"
    assert first[0]["reminder"] == "1/3"
    assert manager.due_reminders(T0 + mins(30) + mins(119)) == []
    second = manager.due_reminders(T0 + mins(30) + mins(120))
    assert second[0]["reminder"] == "2/3"
    third = manager.due_reminders(T0 + mins(30) + mins(240))
    assert third[0]["reminder"] == "3/3"
    assert manager.due_reminders(T0 + mins(30) + mins(999)) == []


def test_state_round_trip_preserves_dedup():
    manager = IncidentManager(SupervisorConfig())
    escalate(manager, T0)
    restored = IncidentManager(SupervisorConfig())
    restored.restore(manager.to_dict())
    event = restored.escalate(
        T0 + mins(1), KEY, "stalled", "critical", T0 - mins(10), SUBJECT, {"m": 2}
    )
    assert event["kind"] == "incident_comment"
