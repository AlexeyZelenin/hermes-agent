"""Corruption forensics (task t_f135b0b6): time-correlate a kanban.db corruption
episode to the writers that were active around detection, enrich the self_heal
engine_log breadcrumb, and surface a Проблема card in the findings store.

All against an isolated board + a throwaway findings DB — never real data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import corruption_forensics as cf
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


def _seed_run_with_events(conn, task_id, run_id, *, pid, outcome, base_at, tools):
    """One task + run + a burst of tool_call events centred on ``base_at``."""
    conn.execute(
        "INSERT OR IGNORE INTO tasks(id,title,status,created_at) VALUES(?,?,?,?)",
        (task_id, task_id, "running", base_at),
    )
    conn.execute(
        "INSERT INTO task_runs(id,task_id,status,worker_pid,outcome,started_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, task_id, "running", pid, outcome, base_at),
    )
    for i, (title, status) in enumerate(tools):
        conn.execute(
            "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) "
            "VALUES(?,?,?,?,?)",
            (task_id, run_id, "tool_call",
             f'{{"title": "{title}", "kind": "edit", "status": "{status}"}}',
             base_at + i),
        )
    conn.commit()


# --- classify ---------------------------------------------------------------


def test_classify_index_only_vs_structural():
    assert cf.classify(["wrong # of entries in index idx_events_run"]) == cf.CLASS_INDEX_ONLY
    assert cf.classify(["row 3 missing from index idx_events_task"]) == cf.CLASS_INDEX_ONLY
    assert cf.classify(["database disk image is malformed"]) == cf.CLASS_STRUCTURAL
    assert cf.classify(["Tree 9 page 448 cell 20: Rowid 18792 out of order"]) == cf.CLASS_STRUCTURAL
    # Mixed → fail closed to structural (any unrecognised row).
    assert cf.classify(
        ["wrong # of entries in index x", "database disk image is malformed"]
    ) == cf.CLASS_STRUCTURAL


# --- snapshot_suspects ------------------------------------------------------


def test_snapshot_finds_windowed_writers_with_run_facts(kanban_home):
    at = 1_000_000
    with kb.connect() as conn:
        # In-window, healthy run.
        _seed_run_with_events(conn, "t_alive", 1, pid=111, outcome=None,
                              base_at=at - 5, tools=[("Read a.py", "completed"),
                                                     ("Edit a.py", "running")])
        # In-window, crashed run (the crash-badge link).
        _seed_run_with_events(conn, "t_dead", 2, pid=222, outcome="crashed",
                              base_at=at + 3, tools=[("Write b.py", "completed")])
        # Out-of-window (older than 60s) — must be excluded.
        _seed_run_with_events(conn, "t_old", 3, pid=333, outcome=None,
                              base_at=at - 500, tools=[("Read c.py", "completed")])

        suspects = cf.snapshot_suspects(conn, at=at)

    ids = {s["task_id"] for s in suspects}
    assert ids == {"t_alive", "t_dead"}
    by_id = {s["task_id"]: s for s in suspects}
    # Sorted worst/closest-first: t_dead (last_at = at+3) before t_alive (at-4).
    assert suspects[0]["task_id"] == "t_dead"
    assert by_id["t_dead"]["crashed"] is True
    assert by_id["t_dead"]["worker_pid"] == 222
    assert by_id["t_alive"]["crashed"] is False
    # Last tool_call carries the human hint from the payload.
    assert by_id["t_alive"]["last_tool_call"]["title"] == "Edit a.py"
    assert by_id["t_alive"]["events"] == 2


def test_snapshot_best_effort_on_bad_connection(kanban_home):
    """A structurally-unreadable connection degrades to an empty suspect list."""
    conn = sqlite3.connect(":memory:")  # no task_events table
    try:
        assert cf.snapshot_suspects(conn, at=1_000_000) == []
    finally:
        conn.close()


# --- emit_episode_finding ---------------------------------------------------


def _findings_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_emit_finding_records_card_with_class_and_suspects():
    conn = _findings_conn()
    try:
        suspects = [
            {"task_id": "t_dead", "run_id": 2, "worker_pid": 222,
             "run_outcome": "crashed", "crashed": True, "events": 1,
             "first_at": 5, "last_at": 5, "last_tool_call": {"title": "Write b.py"}},
            {"task_id": "t_alive", "run_id": 1, "worker_pid": 111,
             "run_outcome": None, "crashed": False, "events": 2,
             "first_at": 1, "last_at": 2, "last_tool_call": {"title": "Edit a.py"}},
        ]
        finding = cf.emit_episode_finding(
            corruption_class=cf.CLASS_INDEX_ONLY, at=1_000_000,
            path="/x/boards/ra/kanban.db", problems=["wrong # of entries in index i"],
            suspects=suspects, healed=True, conn=conn,
        )
        assert finding is not None
        assert finding["severity"] == "error"
        assert "index-only" in finding["title"]
        assert "2 задач" in finding["title"]

        row = conn.execute(
            "SELECT source, category, severity, title, detail, evidence_json "
            "FROM findings"
        ).fetchone()
        assert row["source"] == cf.FINDINGS_SOURCE
        assert row["category"] == "db-corruption"
        # The card body names the culprit tasks and the crash cross-ref.
        assert "t_dead" in row["detail"]
        assert "t_alive" in row["detail"]
        assert "бейджи крашей" in row["detail"] or "бейдж краша" in row["detail"]
        import json
        ev = json.loads(row["evidence_json"])
        assert ev["corruption_class"] == "index-only"
        assert ev["crashed_task_ids"] == ["t_dead"]
    finally:
        conn.close()


def test_emit_finding_structural_is_critical_and_anonymous():
    conn = _findings_conn()
    try:
        finding = cf.emit_episode_finding(
            corruption_class=cf.CLASS_STRUCTURAL, at=1_000_000,
            path="/x/kanban.db", problems=["database disk image is malformed"],
            suspects=[], healed=False, conn=conn,
        )
        assert finding["severity"] == "critical"
        assert "аноним" in finding["title"].lower()
        row = conn.execute("SELECT detail FROM findings").fetchone()
        assert "Аноним" in row["detail"]
    finally:
        conn.close()


def test_emit_finding_upsert_preserves_human_dismissal():
    conn = _findings_conn()
    try:
        kwargs = dict(
            corruption_class=cf.CLASS_INDEX_ONLY, at=1_000_000,
            path="/x/kanban.db", problems=["wrong # of entries in index i"],
            suspects=[], healed=True, conn=conn,
        )
        cf.emit_episode_finding(**kwargs)
        conn.execute("UPDATE findings SET status='dismissed'")
        conn.commit()
        # Re-emitting the SAME episode (same key) must not resurrect it.
        cf.emit_episode_finding(**kwargs)
        rows = conn.execute("SELECT status FROM findings").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "dismissed"
    finally:
        conn.close()


def test_emit_finding_no_store_is_noop(monkeypatch):
    """No zeus.db → no push path, no crash."""
    monkeypatch.setattr(cf, "_open_findings_db", lambda: None)
    assert cf.emit_episode_finding(
        corruption_class=cf.CLASS_INDEX_ONLY, at=1, path="/x", problems=[],
        suspects=[], healed=True,
    ) is None


# --- record_episode (healed path) -------------------------------------------


def test_record_episode_enriches_self_heal_engine_log(kanban_home):
    at = 1_000_000
    with kb.connect() as conn:
        _seed_run_with_events(conn, "t_writer", 1, pid=111, outcome="crashed",
                              base_at=at, tools=[("Edit z.py", "completed")])

        result = cf.record_episode(
            conn, event="kanban.db auto-repaired: REINDEX fixed index corruption",
            severity="error", problems=["wrong # of entries in index idx_events_run"],
            path="/x/kanban.db", at=at,
        )
        assert result["corruption_class"] == "index-only"
        assert any(s["task_id"] == "t_writer" for s in result["suspects"])

        rows = conn.execute(
            "SELECT payload FROM engine_log WHERE category='self_heal'"
        ).fetchall()
    assert len(rows) == 1
    import json
    payload = json.loads(rows[0]["payload"])
    assert payload["corruption_class"] == "index-only"
    assert payload["suspects"][0]["task_id"] == "t_writer"
    assert payload["suspects"][0]["crashed"] is True
