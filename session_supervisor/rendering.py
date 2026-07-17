"""Rendering of escalation events into the triage card and the operator push (contract §3)."""

import json
from datetime import datetime

_ACTION_LABELS = {
    "none": "ничего не предпринято",
    "restarted": "перезапущен",
    "killed": "убит",
}

_COMMENT_HEADS = {
    "incident_comment": "повтор аномалии",
    "incident_reminder": "напоминание",
    "incident_reopened": "инцидент переоткрыт",
}


def minutes_since_anomaly(event: dict) -> int:
    detected = datetime.fromisoformat(event["detected_at"])
    since = datetime.fromisoformat(event["anomaly_since"])
    return max(0, int((detected - since).total_seconds() // 60))


def _action_line(event: dict) -> str:
    last = (event.get("actions_taken") or [{"action": "none"}])[-1]
    action = last.get("action", "none")
    return f"авто: {_ACTION_LABELS.get(action, action)}"


def _loop_line(event: dict) -> str:
    m = event["metrics"]
    head = f"агент зациклился на задаче «{event['task_title']}» ({event['task_id']})"
    if "repeated_tool_calls" in m:
        return f"{head}: {m['repeated_tool_calls']} одинаковых вызовов {m['tool']}"
    return (
        f"{head}: {m['workspace_stale_min']} мин без изменений в workspace, "
        f"+{m['tokens_grown']} токенов"
    )


def _anomaly_line(event: dict) -> str:
    m = event["metrics"]
    title, task_id = event["task_title"], event["task_id"]
    kind = event["anomaly_type"]
    if kind == "stalled":
        minutes = minutes_since_anomaly(event)
        return f"агент застрял на задаче «{title}» ({task_id}), {minutes} мин без прогресса"
    if kind == "loop":
        return _loop_line(event)
    if kind == "token_overspend":
        cap = m.get("token_hard_cap")
        limit = f"жёсткий потолок {cap}" if cap else f"бюджет {m.get('token_budget')}"
        return f"перерасход токенов на задаче «{title}» ({task_id}): {m['tokens_total']}, {limit}"
    if kind == "dispatcher_stall":
        return (
            f"диспетчер не запускает задачи: {m['ready_queue_size']} в очереди ready, "
            f"{m['stalled_ticks']} тиков без запусков"
        )
    return f"аномалия {kind} на задаче «{title}» ({task_id})"


def render_push(event: dict, badge: str) -> str:
    anomaly = _anomaly_line(event)
    if event.get("reminder"):
        anomaly += f" (напоминание {event['reminder']})"
    return "\n".join([f"{badge} {event['board']}".strip(), anomaly, _action_line(event)])


def render_card_title(event: dict) -> str:
    return f"[incident] {event['anomaly_type']}: {event['task_title']} ({event['task_id']})"


def render_card_body(event: dict) -> str:
    return "\n".join([
        f"Инцидент `{event['incident_key']}`",
        "",
        f"- Тип аномалии: {event['anomaly_type']} (severity: {event['severity']})",
        f"- Доска: {event['board']}",
        f"- Задача: «{event['task_title']}» ({event['task_id']}), попытка {event['attempt']}",
        f"- Сессия: {event['session_id']}, воркер: {event['worker_id']}",
        f"- Обнаружено: {event['detected_at']}",
        f"- Аномалия с: {event['anomaly_since']} ({minutes_since_anomaly(event)} мин)",
        f"- Метрики: `{json.dumps(event['metrics'], ensure_ascii=False)}`",
        f"- Авто-действия: `{json.dumps(event['actions_taken'], ensure_ascii=False)}`",
        f"- Лог: `{event['log_ref']}`",
        "",
        _anomaly_line(event),
        _action_line(event),
        "",
        f"event_id: {event['event_id']}",
    ])


def render_comment(event: dict) -> str:
    head = _COMMENT_HEADS.get(event["kind"], event["kind"])
    if event.get("reminder"):
        head += f" {event['reminder']}"
    return "\n".join([
        f"{head} - {event['detected_at']}",
        f"- Метрики: `{json.dumps(event['metrics'], ensure_ascii=False)}`",
        f"- Авто-действия: `{json.dumps(event['actions_taken'], ensure_ascii=False)}`",
        f"- event_id: {event['event_id']}",
    ])


def render_resolve_comment(event: dict, reason: str = "auto: сигнал аномалии ушёл") -> str:
    return "\n".join([
        f"инцидент закрыт - {event['detected_at']}",
        f"- Причина: {reason}",
        f"- event_id: {event['event_id']}",
    ])
