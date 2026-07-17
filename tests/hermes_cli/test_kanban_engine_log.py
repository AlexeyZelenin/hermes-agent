"""Round-trip tests for the engine_log store (t_adf37522) against an isolated
board: record_log / record_client_logs / query_log / prune_engine_log.

The unified log has two producers (operator breadcrumbs + the browser's client
log); this exercises the store edges and the search filters the "под капотом"
viewer and the log-watcher cron rely on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import engine_log as el
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB — never touches real data."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_record_and_query_operator_line(kanban_home):
    with kb.connect() as conn:
        rid = kb.record_log(
            conn, source="operator", event="oversight.scan",
            severity="warn", category="oversight",
            payload={"found": 3}, created_at=1000,
        )
        assert rid > 0
        rows = kb.query_log(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "operator"
    assert row.event == "oversight.scan"
    assert row.severity == "warn"
    assert row.payload == {"found": 3}
    assert row.created_at == 1000


def test_record_log_rejects_unknown_source(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError):
            kb.record_log(conn, source="hacker", event="x")


def test_client_batch_round_trips_through_sanitizer(kanban_home):
    batch = el.sanitize_client_batch(
        [
            {"event": "ws.open", "severity": "info", "category": "ws"},
            {"event": "task.move", "severity": "info", "task_id": "t_1",
             "payload": {"to": "running"}},
            {"no": "event"},  # dropped by the sanitizer
        ],
        now=2000,
        session_id="web-xyz",
    )
    written = kb.record_client_logs(batch)
    assert written == 2
    with kb.connect() as conn:
        rows = kb.query_log(conn, source="client")
    assert {r.event for r in rows} == {"ws.open", "task.move"}
    # session id back-filled from the batch onto every line.
    assert all(r.session_id == "web-xyz" for r in rows)
    move = next(r for r in rows if r.event == "task.move")
    assert move.payload == {"to": "running"}
    assert move.task_id == "t_1"


def test_query_filters_source_severity_task_and_search(kanban_home):
    with kb.connect() as conn:
        kb.record_log(conn, source="operator", event="a", severity="debug")
        kb.record_log(conn, source="operator", event="b", severity="error",
                      task_id="t_9", payload={"note": "boom"})
        kb.record_log(conn, source="client", event="c", severity="info")

        # source filter
        assert {r.event for r in kb.query_log(conn, source="operator")} == {"a", "b"}
        # severity is a *minimum* floor
        assert {r.event for r in kb.query_log(conn, severity="warn")} == {"b"}
        assert {r.event for r in kb.query_log(conn, severity="debug")} == {"a", "b", "c"}
        # task filter
        assert {r.event for r in kb.query_log(conn, task_id="t_9")} == {"b"}
        # substring search over event + payload
        assert {r.event for r in kb.query_log(conn, search="boom")} == {"b"}


def test_query_since_id_acts_as_tail_cursor(kanban_home):
    with kb.connect() as conn:
        first = kb.record_log(conn, source="operator", event="first")
        kb.record_log(conn, source="operator", event="second")
        newer = kb.query_log(conn, since_id=first)
    assert [r.event for r in newer] == ["second"]


def test_query_newest_first_and_limit(kanban_home):
    with kb.connect() as conn:
        for i in range(5):
            kb.record_log(conn, source="operator", event=f"e{i}")
        rows = kb.query_log(conn, limit=2)
    assert [r.event for r in rows] == ["e4", "e3"]


def test_prune_engine_log_drops_old_rows(kanban_home):
    with kb.connect() as conn:
        kb.record_log(conn, source="operator", event="old", created_at=100)
        kb.record_log(conn, source="operator", event="new", created_at=10_000)
        removed = kb.prune_engine_log(conn, older_than_seconds=500, now=10_000)
        assert removed == 1
        rows = kb.query_log(conn)
    assert [r.event for r in rows] == ["new"]
