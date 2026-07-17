"""Rendering per contract §3: push and card texts for every anomaly class."""

from session_supervisor.events import build_event
from session_supervisor.rendering import (
    render_card_body,
    render_card_title,
    render_comment,
    render_push,
    render_resolve_comment,
)
from tests.session_supervisor.util import T0, mins

SUBJECT = {
    "board": "ra",
    "task_id": "t_1",
    "task_title": "Test task",
    "session_id": "s1",
    "worker_id": "host:100",
    "attempt": 2,
    "log_ref": "/logs/r1.jsonl",
}


def event_for(anomaly_type: str, metrics: dict, **overrides) -> dict:
    return build_event(
        overrides.pop("kind", "incident_opened"),
        T0,
        f"ra:t_1:{anomaly_type}",
        anomaly_type,
        overrides.pop("severity", "critical"),
        T0 - mins(30),
        SUBJECT,
        metrics,
    )


def test_stalled_push_has_three_contract_lines():
    event = event_for("stalled", {"heartbeat_silent_min": 30.0, "tokens_silent_min": 30.0})
    lines = render_push(event, "⚡").split("\n")
    assert lines[0] == "⚡ ra"
    assert lines[1] == "агент застрял на задаче «Test task» (t_1), 30 мин без прогресса"
    assert lines[2] == "авто: ничего не предпринято"


def test_loop_push_variants():
    repeats = event_for("loop", {"repeated_tool_calls": 12, "tool": "Bash"})
    assert "12 одинаковых вызовов Bash" in render_push(repeats, "⚡")
    stale = event_for("loop", {"workspace_stale_min": 16.0, "tokens_grown": 40_000})
    assert "16.0 мин без изменений в workspace" in render_push(stale, "⚡")


def test_token_overspend_push_shows_budget_or_cap():
    budget = event_for("token_overspend", {"tokens_total": 900_000, "token_budget": 400_000})
    assert "бюджет 400000" in render_push(budget, "⚡")
    cap = event_for(
        "token_overspend",
        {"tokens_total": 6_000_000, "token_budget": None, "token_hard_cap": 5_000_000},
    )
    assert "жёсткий потолок 5000000" in render_push(cap, "⚡")


def test_dispatcher_stall_push():
    event = event_for("dispatcher_stall", {"ready_queue_size": 4, "stalled_ticks": 3})
    assert "4 в очереди ready" in render_push(event, "⚡")


def test_action_line_reflects_last_auto_action():
    event = event_for("stalled", {"heartbeat_silent_min": 30.0})
    event["actions_taken"] = [{"action": "restarted", "at": "x", "result": "ok"}]
    assert render_push(event, "⚡").endswith("авто: перезапущен")


def test_card_title_and_body_are_self_contained():
    event = event_for("stalled", {"heartbeat_silent_min": 30.0, "tokens_silent_min": 30.0})
    assert render_card_title(event) == "[incident] stalled: Test task (t_1)"
    body = render_card_body(event)
    for fragment in (
        "ra:t_1:stalled",
        "critical",
        "попытка 2",
        "s1",
        "host:100",
        "/logs/r1.jsonl",
        "heartbeat_silent_min",
        event["event_id"],
    ):
        assert fragment in body


def test_comment_and_resolve_render_kind_and_metrics():
    comment = event_for("stalled", {"heartbeat_silent_min": 42.0}, kind="incident_comment")
    text = render_comment(comment)
    assert text.startswith("повтор аномалии")
    assert "42.0" in text
    reminder = event_for("stalled", {"heartbeat_silent_min": 42.0}, kind="incident_reminder")
    reminder["reminder"] = "2/3"
    assert render_comment(reminder).startswith("напоминание 2/3")
    resolved = event_for("stalled", {}, kind="incident_resolved")
    assert "инцидент закрыт" in render_resolve_comment(resolved)
