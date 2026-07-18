"""Tests for hermes_cli.problems — the browse-only "Проблемы" view over the
findings store (task t_e9b93153).

Covers severity folding, row → draft-card shaping, board-scoped listing with
worst-first ordering, dismiss, and accept (materialise a finding into a real
triage backlog card + stamp it ``accepted``). Every test uses an isolated temp
findings db; the accept test spins up an isolated kanban board under a temp
``HERMES_KANBAN_HOME`` — nothing touches a live zeus ledger or kanban store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import integrity_agent as ia
from hermes_cli import problems
from hermes_cli import regular_crons as rc


def _open_store(tmp_path: Path) -> sqlite3.Connection:
    """A fresh findings store with the real schema, isolated to tmp."""
    conn = sqlite3.connect(str(tmp_path / "zeus.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(rc._FINDINGS_SCHEMA)
    return conn


def _emit(conn, *, board, finding_key, title, severity, detail="", now=1000.0):
    rc.emit_finding(
        conn,
        board=board,
        finding_key=finding_key,
        title=title,
        detail=detail,
        category="reliability",
        severity=severity,
        evidence=[],
        now=now,
    )


# --- severity taxonomy ------------------------------------------------------


@pytest.mark.parametrize("sev,rank", [
    ("info", 0), ("low", 1), ("warning", 2), ("warn", 2), ("medium", 2),
    ("high", 3), ("error", 3), ("critical", 4), ("CRITICAL", 4),
    (" High ", 3), ("nonsense", 0), (None, 0), ("", 0),
])
def test_severity_rank(sev, rank):
    assert problems.severity_rank(sev) == rank


@pytest.mark.parametrize("sev,tone", [
    ("info", "info"), ("low", "info"), ("warning", "warning"),
    ("high", "error"), ("critical", "critical"), (None, "info"),
])
def test_severity_tone(sev, tone):
    assert problems.severity_tone(sev) == tone


# --- shaping ----------------------------------------------------------------


def test_problem_from_row_shape(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="k1", title="Boom",
              severity="warning", detail="it broke")
        got = problems.list_problems(conn)
        assert len(got) == 1
        p = got[0]
        assert p["title"] == "Boom"
        assert p["explanation"] == "it broke"       # detail -> explanation
        assert p["proposed"] == ""                   # action_json empty today
        assert p["severity"] == "warning"
        assert p["severity_rank"] == 2
        assert p["tone"] == "warning"
        assert p["board"] == ""
        assert p["status"] == "open"
        assert isinstance(p["evidence"], list)
    finally:
        conn.close()


def test_proposed_pulled_from_action_json(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="k1", title="X", severity="info")
        conn.execute(
            "UPDATE findings SET action_json=? WHERE finding_key='k1'",
            ('{"proposed": "restart the widget"}',),
        )
        conn.commit()
        p = problems.list_problems(conn)[0]
        assert p["proposed"] == "restart the widget"
    finally:
        conn.close()


# --- listing / filtering / ordering -----------------------------------------


def test_list_scopes_by_board(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="g", title="global", severity="info")
        _emit(conn, board="acme", finding_key="a", title="acme", severity="info")
        globals_ = problems.list_problems(conn, board="")
        assert [p["title"] for p in globals_] == ["global"]
        acme = problems.list_problems(conn, board="acme")
        assert [p["title"] for p in acme] == ["acme"]
        both = problems.list_problems(conn, board=None)
        assert {p["title"] for p in both} == {"global", "acme"}
    finally:
        conn.close()


def test_list_orders_worst_first(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="i", title="info", severity="info")
        _emit(conn, board="", finding_key="c", title="crit", severity="critical")
        _emit(conn, board="", finding_key="w", title="warn", severity="warning")
        titles = [p["title"] for p in problems.list_problems(conn, board="")]
        assert titles == ["crit", "warn", "info"]
    finally:
        conn.close()


def test_list_only_open_by_default(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="d", title="dismissed", severity="info")
        problems.dismiss_problem(
            conn, conn.execute("SELECT id FROM findings").fetchone()["id"]
        )
        assert problems.list_problems(conn, board="") == []
    finally:
        conn.close()


def test_list_degrades_without_store():
    assert problems.list_problems(None) == []


def test_list_degrades_without_table(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    conn.row_factory = sqlite3.Row
    try:
        assert problems.list_problems(conn) == []
    finally:
        conn.close()


# --- dismiss ----------------------------------------------------------------


def test_dismiss_marks_and_is_idempotent(tmp_path):
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="k", title="X", severity="info")
        fid = conn.execute("SELECT id FROM findings").fetchone()["id"]
        assert problems.dismiss_problem(conn, fid, now=2000.0) is True
        row = conn.execute(
            "SELECT status, review_status FROM findings WHERE id=?", (fid,)
        ).fetchone()
        assert row["status"] == "dismissed"
        assert row["review_status"] == "dismissed"
        # already dismissed -> no-op
        assert problems.dismiss_problem(conn, fid) is False
    finally:
        conn.close()


def test_dismiss_survives_reemit(tmp_path):
    """A human's dismissal must not be undone by a re-scan (producer upsert)."""
    conn = _open_store(tmp_path)
    try:
        _emit(conn, board="", finding_key="k", title="X", severity="info", now=1000.0)
        fid = conn.execute("SELECT id FROM findings").fetchone()["id"]
        problems.dismiss_problem(conn, fid)
        _emit(conn, board="", finding_key="k", title="X again", severity="warning",
              now=3000.0)  # re-scan
        assert conn.execute(
            "SELECT status FROM findings WHERE id=?", (fid,)
        ).fetchone()["status"] == "dismissed"
    finally:
        conn.close()


# --- accept -----------------------------------------------------------------


def _kanban_board(monkeypatch, tmp_path):
    """Spin up an isolated kanban board under a temp HERMES_KANBAN_HOME."""
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "khome"))
    kanban_db.init_db(board=None)
    return kanban_db.connect(board=None)


def test_accept_creates_triage_card_and_stamps_finding(monkeypatch, tmp_path):
    store = _open_store(tmp_path)
    kanban = _kanban_board(monkeypatch, tmp_path)
    try:
        _emit(store, board="", finding_key="k", title="Fix the flake",
              severity="high", detail="the widget flakes")
        fid = store.execute("SELECT id FROM findings").fetchone()["id"]

        result = problems.accept_problem(store, fid, kanban_conn=kanban)
        assert result is not None
        task_id = result["task_id"]

        # finding is stamped accepted + linked to the new card
        row = store.execute(
            "SELECT status, review_status, converted_task_id FROM findings WHERE id=?",
            (fid,),
        ).fetchone()
        assert row["status"] == "accepted"
        assert row["review_status"] == "accepted"
        assert row["converted_task_id"] == task_id

        # a real triage backlog card now exists carrying the explanation
        from hermes_cli import kanban_db

        task = kanban_db.get_task(kanban, task_id)
        assert task is not None
        assert task.status == "triage"
        assert task.title == "Fix the flake"
        assert "the widget flakes" in (task.body or "")

        # no longer in the browse view
        assert problems.list_problems(store, board="") == []
    finally:
        store.close()
        kanban.close()


def test_accept_survives_reemit(monkeypatch, tmp_path):
    """An accepted finding must stay accepted through a re-scan."""
    store = _open_store(tmp_path)
    kanban = _kanban_board(monkeypatch, tmp_path)
    try:
        _emit(store, board="", finding_key="k", title="X", severity="warning",
              now=1000.0)
        fid = store.execute("SELECT id FROM findings").fetchone()["id"]
        problems.accept_problem(store, fid, kanban_conn=kanban)
        _emit(store, board="", finding_key="k", title="X again", severity="high",
              now=3000.0)  # re-scan
        assert store.execute(
            "SELECT status FROM findings WHERE id=?", (fid,)
        ).fetchone()["status"] == "accepted"
    finally:
        store.close()
        kanban.close()


def test_accept_missing_or_resolved_returns_none(tmp_path):
    store = _open_store(tmp_path)
    try:
        assert problems.accept_problem(store, 999) is None
    finally:
        store.close()


def test_integrity_upsert_preserves_accepted(tmp_path):
    """The integrity producer must also preserve an accepted human decision."""
    conn = _open_store(tmp_path)
    try:
        ia.emit_finding(
            conn,
            board="",
            finding={"finding_key": "k", "title": "X", "detail": "",
                     "evidence": [], "category": "drift", "severity": "warning"},
            now=1000.0,
        )
        fid = conn.execute("SELECT id FROM findings").fetchone()["id"]
        conn.execute("UPDATE findings SET status='accepted' WHERE id=?", (fid,))
        conn.commit()
        ia.emit_finding(
            conn,
            board="",
            finding={"finding_key": "k", "title": "X2", "detail": "",
                     "evidence": [], "category": "drift", "severity": "high"},
            now=2000.0,
        )
        assert conn.execute(
            "SELECT status FROM findings WHERE id=?", (fid,)
        ).fetchone()["status"] == "accepted"
    finally:
        conn.close()
