"""End-to-end escalation: Supervisor -> EscalationRouter -> real hermes kanban DB.

Drives the real ``hermes_cli.kanban_db`` (temp database, sandboxed HERMES_KANBAN_HOME)
through KanbanTaskGateway. Proves the acceptance criterion: for each anomaly class one
anomaly yields exactly one triage card and one push with full context, repeats become
comments, resolution closes the card, and delivery survives restarts and transient
channel errors without duplicates. Skipped when the hermes-agent repo is not available.
"""

import os
import sys
from pathlib import Path

import pytest

from session_supervisor import Supervisor, SupervisorConfig
from session_supervisor.escalation import EscalationRouter
from session_supervisor.kanban_gateway import KanbanTaskGateway
from tests.session_supervisor.test_escalation import FakeNotifier
from tests.session_supervisor.util import T0, dead_prober, looping_calls, make_run, mins, stalled_dispatcher

HERMES_REPO = Path(
    os.environ.get("HERMES_AGENT_REPO", "~/.hermes/hermes-agent")
).expanduser()

if str(HERMES_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_REPO))
kdb = pytest.importorskip("hermes_cli.kanban_db")

CONFIG = SupervisorConfig(loop_poll_allowlist=("TaskOutput",))


def anomalous_run(anomaly_type: str, now: float, **overrides):
    if anomaly_type == "stalled":
        overrides.setdefault("last_heartbeat_at", now - mins(10))
        overrides.setdefault("last_token_usage_at", now - mins(10))
    elif anomaly_type == "loop":
        overrides.setdefault("recent_tool_calls", looping_calls(10))
    elif anomaly_type == "token_overspend":
        overrides.setdefault("tokens_total", 900_000)
        overrides.setdefault("token_budget", 400_000)
    return make_run(now, **overrides)


class Env:
    """One temp board: real kanban DB + supervisor + router with persisted state."""

    def __init__(self, tmp_path: Path):
        self.db_path = tmp_path / "kanban.db"
        self.tmp_path = tmp_path
        self.notifier = FakeNotifier()
        self.supervisor = self._make_supervisor()
        self.router = self._make_router()

    def _make_supervisor(self) -> Supervisor:
        return Supervisor(
            config=CONFIG,
            prober=dead_prober,
            state_path=str(self.tmp_path / "supervisor-state.json"),
        )

    def _make_router(self) -> EscalationRouter:
        gateway = KanbanTaskGateway(
            "testboard", db_path=self.db_path, hermes_repo=str(HERMES_REPO)
        )
        return EscalationRouter(
            gateway,
            self.notifier,
            state_path=str(self.tmp_path / "escalation-state.json"),
            badge="⚡",
        )

    def restart(self) -> None:
        self.supervisor = self._make_supervisor()
        self.router = self._make_router()

    def tick(self, now: float, runs, dispatcher=None) -> list[dict]:
        events = self.supervisor.tick(now, runs, dispatcher)
        self.router.route(now, events)
        return events

    def incident_tasks(self) -> list:
        conn = kdb.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE title LIKE '[incident]%' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def comments(self, task_id: str) -> list:
        conn = kdb.connect(self.db_path)
        try:
            return kdb.list_comments(conn, task_id)
        finally:
            conn.close()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return Env(tmp_path)


@pytest.mark.parametrize("anomaly_type", ["stalled", "loop", "token_overspend"])
def test_one_anomaly_one_card_one_push_full_lifecycle(env, anomaly_type):
    env.tick(T0, [anomalous_run(anomaly_type, T0)])  # suspected, no escalation yet
    assert env.incident_tasks() == []
    env.tick(T0 + 60, [anomalous_run(anomaly_type, T0 + 60)])  # confirmed -> opened

    cards = env.incident_tasks()
    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "triage"
    assert card["title"].startswith(f"[incident] {anomaly_type}: Test task (t_1)")
    for fragment in (f"ra:t_1:{anomaly_type}", "host:100", "s1", "/logs/r1.jsonl"):
        assert fragment in card["body"]
    assert len(env.notifier.sent) == 1
    assert env.notifier.sent[0].startswith("⚡ ra\n")
    assert "(t_1)" in env.notifier.sent[0]

    # While confirmed, further anomalous ticks are deduped upstream: no new events.
    env.tick(T0 + 120, [anomalous_run(anomaly_type, T0 + 120)])
    assert len(env.incident_tasks()) == 1
    assert len(env.notifier.sent) == 1
    assert env.comments(card["id"]) == []

    # Run drops out of the scan, comes back anomalous, is re-confirmed:
    # the still-open incident gets a comment, not a second card or push.
    env.tick(T0 + 180, [])
    env.tick(T0 + 240, [anomalous_run(anomaly_type, T0 + 240)])
    env.tick(T0 + 300, [anomalous_run(anomaly_type, T0 + 300)])
    assert len(env.incident_tasks()) == 1
    assert len(env.notifier.sent) == 1
    comments = env.comments(card["id"])
    assert len(comments) == 1
    assert "повтор аномалии" in comments[0].body

    env.tick(T0 + 360, [make_run(T0 + 360)])  # healthy -> auto-resolve, no push
    card = env.incident_tasks()[0]
    assert card["status"] == "archived"
    assert len(env.notifier.sent) == 1
    assert any("инцидент закрыт" in c.body for c in env.comments(card["id"]))


def test_dispatcher_stall_escalates_end_to_end(env):
    for i in range(3):
        env.tick(T0 + 60 * i, [], stalled_dispatcher())
    cards = env.incident_tasks()
    assert len(cards) == 1
    assert "dispatcher_stall" in cards[0]["title"]
    assert len(env.notifier.sent) == 1
    assert "диспетчер не запускает задачи" in env.notifier.sent[0]


def test_restart_and_redelivery_do_not_duplicate(env):
    env.tick(T0, [anomalous_run("stalled", T0)])
    events = env.tick(T0 + 60, [anomalous_run("stalled", T0 + 60)])
    assert len(env.incident_tasks()) == 1

    env.restart()  # supervisor + router reload persisted state
    env.router.route(T0 + 90, events)  # host redelivers the same events
    env.tick(T0 + 120, [anomalous_run("stalled", T0 + 120)])
    assert len(env.incident_tasks()) == 1
    assert len(env.notifier.sent) == 1


def test_reopen_within_window_reuses_the_card_and_pushes(env):
    env.tick(T0, [anomalous_run("stalled", T0)])
    env.tick(T0 + 60, [anomalous_run("stalled", T0 + 60)])
    env.tick(T0 + 120, [make_run(T0 + 120)])  # resolve -> archived
    assert env.incident_tasks()[0]["status"] == "archived"

    t1 = T0 + mins(10)  # inside the 30 min reopen window
    env.tick(t1, [anomalous_run("stalled", t1)])
    env.tick(t1 + 60, [anomalous_run("stalled", t1 + 60)])  # confirmed -> reopened
    cards = env.incident_tasks()
    assert len(cards) == 1
    assert cards[0]["status"] == "triage"
    assert len(env.notifier.sent) == 2
    assert any("инцидент переоткрыт" in c.body for c in env.comments(cards[0]["id"]))


def test_transient_push_failure_delivers_exactly_once(env):
    env.notifier.fail_next = 1
    env.tick(T0, [anomalous_run("loop", T0)])
    env.tick(T0 + 60, [anomalous_run("loop", T0 + 60)])
    assert len(env.incident_tasks()) == 1
    assert env.notifier.sent == []  # first attempt failed, recorded as pending
    env.router.flush(T0 + 60 + 61)  # past the retry backoff
    assert len(env.notifier.sent) == 1
    env.router.flush(T0 + 60 + 200)
    assert len(env.notifier.sent) == 1


def test_reminder_pushes_again_with_mark(env):
    env.tick(T0, [anomalous_run("stalled", T0)])
    env.tick(T0 + 60, [anomalous_run("stalled", T0 + 60)])
    t_reminder = T0 + 60 + mins(31)
    env.tick(t_reminder, [anomalous_run("stalled", t_reminder)])
    assert len(env.notifier.sent) == 2
    assert "напоминание 1/3" in env.notifier.sent[1]
    assert len(env.incident_tasks()) == 1
