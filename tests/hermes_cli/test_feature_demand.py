"""Tests for hermes_cli.feature_demand — cross-card demand advisor signal.

The pure detector (:func:`compute_demand`) is exercised with plain card dicts +
link pairs (no board or ledger); the findings store uses an isolated temp
zeus.db; the real wiring (:func:`load_board_cards` / :func:`run_feature_demand_scan`)
runs against the per-test isolated kanban home (the autouse hermetic fixture in
tests/conftest.py). Nothing here touches a live board or the real ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import feature_demand as fd


# ---------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------


def test_extract_mentions_finds_task_ids():
    text = "нужен paused-флаг t_d553512c и ещё t_abc12345, но не t_ (голый)."
    assert fd.extract_mentions(text) == {"t_d553512c", "t_abc12345"}


def test_extract_mentions_case_insensitive_and_empty():
    assert fd.extract_mentions("See T_ABC12345") == {"t_abc12345"}
    assert fd.extract_mentions(None) == set()
    assert fd.extract_mentions("no ids here") == set()


# ---------------------------------------------------------------------------
# compute_demand (pure)
# ---------------------------------------------------------------------------


def _card(cid, *, status="todo", body="", title=None, priority=0):
    return {"id": cid, "title": title or cid, "body": body,
            "status": status, "priority": priority, "paused": False}


def test_demand_fires_when_several_cards_mention_one_feature():
    # The night-shift example: three open cards reference an unbuilt feature.
    cards = [
        _card("t_fea70000", status="todo", title="paused-флаг"),
        _card("t_a0000001", body="блокирует t_fea70000"),
        _card("t_a0000002", body="ждёт t_fea70000 как старый ON-HOLD"),
        _card("t_a0000003", body="talk-to-task зависит от t_fea70000"),
    ]
    demand = fd.compute_demand(cards, min_waiters=2)
    assert len(demand) == 1
    d = demand[0]
    assert d["feature_id"] == "t_fea70000"
    assert d["waiter_count"] == 3
    assert set(d["evidence"]["waiter_ids"]) == {"t_a0000001", "t_a0000002", "t_a0000003"}
    assert d["severity"] == "info"
    assert d["category"] == "prioritization"
    assert "поднять приоритет" in d["title"]


def test_demand_silent_below_threshold():
    cards = [
        _card("t_fea70000", status="todo"),
        _card("t_b0000001", body="one waiter for t_fea70000"),
    ]
    assert fd.compute_demand(cards, min_waiters=2) == []


def test_demand_ignores_reference_to_built_feature():
    # A done feature is nothing to prioritize, even with many waiters.
    cards = [
        _card("t_fea70000", status="done"),
        _card("t_c0000001", body="t_fea70000"),
        _card("t_c0000002", body="t_fea70000"),
    ]
    assert fd.compute_demand(cards, min_waiters=2) == []


def test_demand_ignores_running_feature_already_being_built():
    cards = [
        _card("t_fea70000", status="running"),
        _card("t_c0000001", body="t_fea70000"),
        _card("t_c0000002", body="t_fea70000"),
    ]
    assert fd.compute_demand(cards, min_waiters=2) == []


def test_paused_feature_still_surfaces():
    # paused is orthogonal to status: a paused todo is exactly the raise/unpause case.
    cards = [
        {"id": "t_fea70000", "title": "F", "body": "", "status": "todo",
         "priority": 0, "paused": True},
        _card("t_c0000001", body="t_fea70000"),
        _card("t_c0000002", body="t_fea70000"),
    ]
    demand = fd.compute_demand(cards, min_waiters=2)
    assert len(demand) == 1 and demand[0]["waiter_count"] == 2


def test_demand_only_counts_open_waiters():
    cards = [
        _card("t_fea70000", status="todo"),
        _card("t_c0000001", body="t_fea70000"),
        _card("t_d0000009", status="done", body="t_fea70000"),  # finished, not waiting
    ]
    assert fd.compute_demand(cards, min_waiters=2) == []


def test_demand_ignores_mention_of_unknown_id():
    # A referenced id that is not an open card on the board can't be raised.
    cards = [
        _card("t_c0000001", body="t_e0000000"),
        _card("t_c0000002", body="t_e0000000"),
    ]
    assert fd.compute_demand(cards, min_waiters=2) == []


def test_demand_excludes_self_reference():
    cards = [
        _card("t_fea70000", status="todo", body="see t_fea70000 itself"),
        _card("t_c0000001", body="t_fea70000"),
    ]
    # Only the real waiter counts; self-mention does not.
    assert fd.compute_demand(cards, min_waiters=1)[0]["waiter_count"] == 1


def test_demand_link_channel_and_dedup_with_mention():
    cards = [
        _card("t_fea70000", status="todo"),
        _card("t_c0000001", body="t_fea70000"),               # mention
        _card("t_c0000002", body="unrelated"),                 # link only
    ]
    links = [("t_fea70000", "t_c0000002"), ("t_fea70000", "t_c0000001")]  # w1 both channels
    demand = fd.compute_demand(cards, links=links, min_waiters=2)
    assert demand[0]["waiter_count"] == 2
    via = demand[0]["evidence"]["via"]
    assert set(via["mention"]) == {"t_c0000001"}
    assert set(via["link"]) == {"t_c0000001", "t_c0000002"}


def test_demand_sorted_by_waiter_count_desc():
    cards = [
        _card("t_fa000000", status="todo"),
        _card("t_fb000000", status="todo"),
        _card("t_c0000001", body="t_fa000000 t_fb000000"),
        _card("t_c0000002", body="t_fa000000 t_fb000000"),
        _card("t_c0000003", body="t_fa000000"),  # a has 3, b has 2
    ]
    demand = fd.compute_demand(cards, min_waiters=2)
    assert [d["feature_id"] for d in demand] == ["t_fa000000", "t_fb000000"]


def test_ru_plural():
    assert fd._ru_plural(1, "карту", "карты", "карт") == "карту"
    assert fd._ru_plural(2, "карту", "карты", "карт") == "карты"
    assert fd._ru_plural(5, "карту", "карты", "карт") == "карт"
    assert fd._ru_plural(11, "карту", "карты", "карт") == "карт"
    assert fd._ru_plural(21, "карту", "карты", "карт") == "карту"


# ---------------------------------------------------------------------------
# Findings store: emit / clear / scan_and_emit
# ---------------------------------------------------------------------------


def _open_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _demand_one():
    cards = [
        _card("t_fea70000", status="todo", title="paused-флаг"),
        _card("t_c0000001", body="t_fea70000"),
        _card("t_c0000002", body="t_fea70000"),
    ]
    return fd.compute_demand(cards, min_waiters=2)


def test_emit_and_clear_roundtrip(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    d = _demand_one()[0]
    fd.emit_finding(conn, d, board="ra", now=1000.0)

    row = conn.execute("SELECT * FROM findings WHERE source='feature-demand'").fetchone()
    assert row["finding_key"] == "feature-demand:t_fea70000"
    assert row["status"] == "open"
    assert row["board"] == "ra"
    assert row["category"] == "prioritization"

    fd.clear_finding(conn, board="ra", finding_key=d["finding_key"], now=2000.0)
    row = conn.execute("SELECT status FROM findings WHERE finding_key=?",
                       (d["finding_key"],)).fetchone()
    assert row["status"] == "obsolete"
    conn.close()


def test_emit_preserves_human_dismissal(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    d = _demand_one()[0]
    fd.emit_finding(conn, d, board="ra", now=1000.0)
    conn.execute("UPDATE findings SET status='dismissed' WHERE finding_key=?",
                 (d["finding_key"],))
    conn.commit()
    # Re-emitting must not resurrect a human-dismissed finding.
    fd.emit_finding(conn, d, board="ra", now=1500.0)
    row = conn.execute("SELECT status FROM findings WHERE finding_key=?",
                       (d["finding_key"],)).fetchone()
    assert row["status"] == "dismissed"
    conn.close()


def test_scan_and_emit_clears_faded_demand(tmp_path):
    conn = _open_store(tmp_path / "zeus.db")
    fd.scan_and_emit(_demand_one(), conn, board="ra", now=1000.0)
    assert conn.execute(
        "SELECT status FROM findings WHERE finding_key='feature-demand:t_fea70000'"
    ).fetchone()["status"] == "open"

    # Next pass: demand gone (feature built / waiters done) -> the open one clears.
    fd.scan_and_emit([], conn, board="ra", now=2000.0)
    assert conn.execute(
        "SELECT status FROM findings WHERE finding_key='feature-demand:t_fea70000'"
    ).fetchone()["status"] == "obsolete"
    conn.close()


def test_scan_and_emit_board_scoped_clear(tmp_path):
    # A finding on another board must not be cleared by this board's empty pass.
    conn = _open_store(tmp_path / "zeus.db")
    fd.scan_and_emit(_demand_one(), conn, board="ra", now=1000.0)
    fd.scan_and_emit(_demand_one(), conn, board="zeus", now=1000.0)
    fd.scan_and_emit([], conn, board="ra", now=2000.0)
    statuses = {
        r["board"]: r["status"]
        for r in conn.execute("SELECT board, status FROM findings").fetchall()
    }
    assert statuses == {"ra": "obsolete", "zeus": "open"}
    conn.close()


def test_scan_and_emit_noop_without_conn():
    assert fd.scan_and_emit(_demand_one(), None, board="ra") == []


# ---------------------------------------------------------------------------
# Real wiring against an isolated kanban board
# ---------------------------------------------------------------------------


def test_load_board_cards_and_scan_end_to_end(tmp_path):
    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    try:
        fid = kanban_db.create_task(conn, title="paused-флаг", triage=True)
        kanban_db.create_task(conn, title="rename", body=f"нужен {fid}", triage=True)
        kanban_db.create_task(conn, title="talk-to-task",
                              body=f"зависит от {fid}", triage=True)
    finally:
        conn.close()

    cards, links = fd.load_board_cards()
    ids = {c["id"] for c in cards}
    assert fid in ids and len(cards) == 3

    store = _open_store(tmp_path / "zeus.db")
    try:
        demand = fd.run_feature_demand_scan(conn=store, min_waiters=2)
    finally:
        store.close()
    assert len(demand) == 1
    assert demand[0]["feature_id"] == fid
    assert demand[0]["waiter_count"] == 2

    row = _open_store(tmp_path / "zeus.db").execute(
        "SELECT status FROM findings WHERE finding_key=?",
        (fd.finding_key(fid),)).fetchone()
    assert row is not None and row["status"] == "open"


def test_load_board_cards_link_channel(tmp_path):
    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    try:
        fid = kanban_db.create_task(conn, title="feature", triage=True)
        # Two children linked to the feature — the board knows they wait on it.
        kanban_db.create_task(conn, title="child A", parents=[fid])
        kanban_db.create_task(conn, title="child B", parents=[fid])
    finally:
        conn.close()

    cards, links = fd.load_board_cards()
    assert (fid, ) in {(p,) for p, _ in links}
    demand = fd.compute_demand(cards, links=links, min_waiters=2)
    assert len(demand) == 1 and demand[0]["feature_id"] == fid
    assert set(demand[0]["evidence"]["via"]["link"]) == {
        c["id"] for c in cards if c["id"] != fid
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_no_emit_empty_board_returns_zero(capsys):
    rc = fd.main(["--no-emit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ни одна невыстроенная фича" in out


def test_main_json_output(capsys):
    rc = fd.main(["--no-emit", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"demand_count": 0' in out
