from __future__ import annotations

from pathlib import Path

from zeus_watchdog.checks import Condition
from zeus_watchdog.state import (
    PROBLEM,
    RECOVERY,
    load_state,
    reconcile,
    save_state,
)

DEBOUNCE = 1800


def _c(key, summary="x", sustain=0):
    return Condition(key, summary, sustain)


def test_instant_condition_alerts_immediately():
    state, alerts = reconcile({}, [_c("gateway_dead")], now=100.0, debounce_sec=DEBOUNCE)
    assert [(a.kind, a.key) for a in alerts] == [(PROBLEM, "gateway_dead")]
    assert state["gateway_dead"]["alerted"] is True


def test_sustain_gate_holds_then_fires():
    conds = [_c("ready_no_run", sustain=600)]
    # First sight at t=0: recorded, not yet alerted.
    s1, a1 = reconcile({}, conds, now=0.0, debounce_sec=DEBOUNCE)
    assert a1 == []
    assert s1["ready_no_run"]["alerted"] is False
    # Still within the sustain window.
    s2, a2 = reconcile(s1, conds, now=300.0, debounce_sec=DEBOUNCE)
    assert a2 == []
    # Past the sustain window -> fires.
    s3, a3 = reconcile(s2, conds, now=601.0, debounce_sec=DEBOUNCE)
    assert [a.key for a in a3] == ["ready_no_run"]
    assert s3["ready_no_run"]["since"] == 0.0  # since is preserved across passes


def test_debounce_blocks_repeat_then_allows():
    conds = [_c("gateway_dead")]
    s1, a1 = reconcile({}, conds, now=0.0, debounce_sec=DEBOUNCE)
    assert len(a1) == 1
    # Within debounce: silent.
    s2, a2 = reconcile(s1, conds, now=1000.0, debounce_sec=DEBOUNCE)
    assert a2 == []
    # After debounce elapses: re-alert.
    s3, a3 = reconcile(s2, conds, now=1801.0, debounce_sec=DEBOUNCE)
    assert [a.key for a in a3] == ["gateway_dead"]


def test_recovery_emitted_when_cleared():
    s1, _ = reconcile({}, [_c("gateway_dead")], now=0.0, debounce_sec=DEBOUNCE)
    s2, a2 = reconcile(s1, [], now=50.0, debounce_sec=DEBOUNCE)
    assert [(a.kind, a.key) for a in a2] == [(RECOVERY, "gateway_dead")]
    assert "gateway_dead" not in s2  # resolved, dropped


def test_no_recovery_if_never_alerted():
    # Condition seen once but cleared before its sustain window: no alert, no recovery.
    s1, a1 = reconcile({}, [_c("ready_no_run", sustain=600)], now=0.0, debounce_sec=DEBOUNCE)
    assert a1 == []
    s2, a2 = reconcile(s1, [], now=100.0, debounce_sec=DEBOUNCE)
    assert a2 == []
    assert s2 == {}


def test_save_load_roundtrip(tmp_path: Path):
    path = tmp_path / "nested" / "state.json"
    problems = {
        "gateway_dead": {"since": 1.0, "alerted": True, "last_alerted": 1.0, "summary": "x"},
    }
    save_state(path, problems)
    assert load_state(path) == problems


def test_load_missing_or_garbage_returns_empty(tmp_path: Path):
    assert load_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_state(bad) == {}
