"""Tests for hermes_cli.vision_reconcile — backlog↔vision drift reconciliation.

Pure detector + findings-store behaviour against a temp sqlite db. The judge is
injected (a plain function), so no network or real provider is touched. The
findings-emission path is exercised against a throwaway db, then read back
through hermes_cli.problems to prove drift surfaces in Проблемы.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import vision_reconcile as vr

VISION = "# Vision\n\n## Что НЕ делаем\n- не полурешения\n"

TASKS = [
    {"id": "t_a", "title": "Local repo and that's it", "body": "b", "status": "todo"},
    {"id": "t_b", "title": "Finish the pipeline", "body": "b", "status": "ready"},
]


def _judge_flags(flags):
    """Build a judge that returns a fixed drift list, ignoring inputs."""
    return lambda tasks, vision: list(flags)


# --- reconcile (pure) -------------------------------------------------------


def test_reconcile_shapes_valid_drift():
    judge = _judge_flags([{"task_id": "t_a", "kind": "orphan", "reason": "half"}])
    findings = vr.reconcile(TASKS, VISION, judge)
    assert len(findings) == 1
    f = findings[0]
    assert f["task_id"] == "t_a"
    assert f["kind"] == vr.KIND_ORPHAN
    assert f["finding_key"] == "vision:t_a:orphan"
    assert "half" in f["detail"]
    assert f["severity"] == "warning"


def test_reconcile_drops_hallucinated_task_id():
    judge = _judge_flags([{"task_id": "t_ghost", "kind": "orphan", "reason": "x"}])
    assert vr.reconcile(TASKS, VISION, judge) == []


def test_reconcile_drops_unknown_kind():
    judge = _judge_flags([{"task_id": "t_a", "kind": "bogus", "reason": "x"}])
    assert vr.reconcile(TASKS, VISION, judge) == []


def test_reconcile_dedupes_task_kind_pairs():
    judge = _judge_flags([
        {"task_id": "t_a", "kind": "orphan", "reason": "one"},
        {"task_id": "t_a", "kind": "orphan", "reason": "two"},
    ])
    assert len(vr.reconcile(TASKS, VISION, judge)) == 1


def test_reconcile_no_vision_or_no_tasks_is_empty():
    judge = _judge_flags([{"task_id": "t_a", "kind": "orphan", "reason": "x"}])
    assert vr.reconcile(TASKS, "", judge) == []
    assert vr.reconcile([], VISION, judge) == []


def test_reconcile_swallows_judge_error():
    def boom(tasks, vision):
        raise RuntimeError("judge exploded")

    assert vr.reconcile(TASKS, VISION, boom) == []


# --- findings store ---------------------------------------------------------


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_scan_and_emit_pushes_and_clears(db):
    # First pass: t_a drifts -> one open finding.
    judge = _judge_flags([{"task_id": "t_a", "kind": "orphan", "reason": "half"}])
    findings = vr.reconcile(TASKS, VISION, judge)
    vr.scan_and_emit(TASKS, findings, db, board="ra")
    rows = db.execute(
        "SELECT finding_key, status FROM findings WHERE source='vision'"
    ).fetchall()
    assert [(r["finding_key"], r["status"]) for r in rows] == [("vision:t_a:orphan", "open")]

    # Second pass: no drift -> the finding is cleared (obsolete), not deleted.
    vr.scan_and_emit(TASKS, [], db, board="ra")
    status = db.execute(
        "SELECT status FROM findings WHERE finding_key='vision:t_a:orphan'"
    ).fetchone()["status"]
    assert status == "obsolete"


def test_emit_preserves_human_dismissal(db):
    judge = _judge_flags([{"task_id": "t_a", "kind": "orphan", "reason": "half"}])
    findings = vr.reconcile(TASKS, VISION, judge)
    vr.scan_and_emit(TASKS, findings, db, board="ra")
    db.execute("UPDATE findings SET status='dismissed' WHERE source='vision'")
    db.commit()
    # Re-emitting the same drift must NOT resurrect a human-dismissed finding.
    vr.scan_and_emit(TASKS, findings, db, board="ra")
    status = db.execute(
        "SELECT status FROM findings WHERE finding_key='vision:t_a:orphan'"
    ).fetchone()["status"]
    assert status == "dismissed"


def test_scan_and_emit_none_conn_is_noop():
    judge = _judge_flags([{"task_id": "t_a", "kind": "orphan", "reason": "x"}])
    findings = vr.reconcile(TASKS, VISION, judge)
    assert vr.scan_and_emit(TASKS, findings, None, board="ra") == []


def test_findings_surface_in_problems(db):
    problems = pytest.importorskip("hermes_cli.problems")
    judge = _judge_flags([
        {"task_id": "t_a", "kind": "orphan", "reason": "half-solution"},
    ])
    findings = vr.reconcile(TASKS, VISION, judge)
    vr.scan_and_emit(TASKS, findings, db, board="ra")
    listed = problems.list_problems(db, board="ra")
    assert len(listed) == 1
    assert listed[0]["source"] == "vision"
    assert listed[0]["category"] == "vision-drift"


# --- judge-reply parsing ----------------------------------------------------


def test_parse_judge_reply_extracts_findings():
    raw = 'prose {"findings": [{"task_id": "t_a", "kind": "orphan"}]} trailing'
    out = vr._parse_judge_reply(raw)
    assert out == [{"task_id": "t_a", "kind": "orphan"}]


def test_parse_judge_reply_tolerates_garbage():
    assert vr._parse_judge_reply("") == []
    assert vr._parse_judge_reply("no json here") == []
    assert vr._parse_judge_reply('{"findings": "notalist"}') == []
