"""Budget-scenario coverage for the soft context-budget handoff (t_af653fd4).

The unit-level policy is exercised in ``test_soft_handoff.py``. This suite is
scenario-driven: it drives the real ``goals.run_kanban_goal_loop`` through the
handoff situations an operator actually cares about and asserts, deterministically:

  1. no handoff while occupancy stays below the soft threshold,
  2. a handoff right around the 60% threshold (incl. the exact boundary),
  3. no *repeated* handoff once the fresh session's window recovers,
  4. faithful save AND load of the handoff spec-state (disk round-trip),
  5. graceful continue-in-session when Hermes context metrics are unavailable,
  6. the hard turn-budget enforcement is unchanged while soft handoff is active,
  7. the exact call ORDER inside a handoff (read occupancy -> write spec ->
     reset session -> run turn).

Everything runs through injected callbacks and tmp dirs - no live model and no
live kanban board (per protocol: a board-touching test would spin up an
isolated ``HERMES_KANBAN_HOME``; this suite needs no board at all).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import soft_handoff as sh

HANDOFF_MARKER = "SOFT CONTEXT HANDOFF"


def _patch_judge(monkeypatch, verdicts):
    """Force ``judge_goal`` to replay a scripted verdict sequence so the loop is
    fully deterministic (no model call). The judge ``reason`` becomes the spec's
    ``next_step``, which several assertions below rely on."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        return v, f"scripted:{v}", False, None

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def _cfg(soft_pct=0.60, max_handoffs=2, enabled=True):
    return sh.SoftHandoffConfig(enabled=enabled, soft_pct=soft_pct, max_handoffs=max_handoffs)


# ---------------------------------------------------------------------------
# 1. No handoff below the soft threshold
# ---------------------------------------------------------------------------

def test_no_handoff_while_occupancy_below_threshold(monkeypatch, tmp_path):
    _patch_judge(monkeypatch, ["continue", "continue", "continue"])
    statuses = iter(["running", "running", "running", "done"])
    # Occupancy stays comfortably under 0.60 for every continuation turn.
    occ = iter([0.40, 0.55, 0.59])
    resets = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="below", goal_text="ship",
        run_turn=lambda p: turns.append(p) or "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="started",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=lambda: next(occ),
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    assert resets == []
    assert all(HANDOFF_MARKER not in p for p in turns)
    # Nothing was checkpointed.
    assert not (tmp_path / "handoff").exists() or not list((tmp_path / "handoff").glob("*.json"))


# ---------------------------------------------------------------------------
# 2. Handoff right around 60% (boundary is inclusive: >= threshold)
# ---------------------------------------------------------------------------

def test_evaluate_boundary_is_inclusive():
    cfg = _cfg()
    # Exactly at the threshold hands off; a hair under does not.
    assert sh.evaluate(0.60, compaction_active=False, handoffs_done=0, config=cfg).should_handoff
    below = sh.evaluate(0.5999, compaction_active=False, handoffs_done=0, config=cfg)
    assert not below.should_handoff
    assert below.code == sh.CODE_BELOW_THRESHOLD


def test_goal_loop_hands_off_at_exact_threshold(monkeypatch, tmp_path):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    # First continuation sits exactly on 0.60; the fresh session drops to 0.10.
    occ = iter([0.60, 0.10])
    resets = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="boundary", goal_text="ship",
        run_turn=lambda p: turns.append(p) or "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="started",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=lambda: next(occ),
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    assert resets == [True]
    assert HANDOFF_MARKER in turns[0]
    assert HANDOFF_MARKER not in turns[1]


# ---------------------------------------------------------------------------
# 3. No repeated handoff once the fresh session has recovered
# ---------------------------------------------------------------------------

def test_no_repeat_handoff_after_recovery(monkeypatch, tmp_path):
    """A single spike triggers one handoff; the fresh window recovers and stays
    low, so no further handoff fires even across several more turns. This is
    distinct from the cap: with max_handoffs=2 the cap is never the limiter -
    recovery is."""
    _patch_judge(monkeypatch, ["continue"] * 4)
    statuses = iter(["running", "running", "running", "running", "done"])
    # Spike once, then recover and hold well below threshold.
    occ = iter([0.80, 0.25, 0.25, 0.25])
    resets = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="recover", goal_text="ship",
        run_turn=lambda p: turns.append(p) or "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=20, first_response="started",
        soft_handoff_config=_cfg(max_handoffs=2),
        context_occupancy_fn=lambda: next(occ),
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    # Exactly one reset despite headroom left under the cap (2).
    assert resets == [True]
    assert HANDOFF_MARKER in turns[0]
    assert all(HANDOFF_MARKER not in p for p in turns[1:])
    # Only the single first-handoff spec exists.
    specs = list((tmp_path / "handoff").glob("*.json"))
    assert len(specs) == 1


# ---------------------------------------------------------------------------
# 4. Save AND load of the spec-state (disk round-trip; passed-state content)
# ---------------------------------------------------------------------------

def test_spec_state_saved_and_loaded_roundtrip(monkeypatch, tmp_path):
    """Drive one handoff, then LOAD the spec back from disk and assert the
    persisted state is exactly what was live at handoff time - the acceptance
    criterion 'содержимое передаваемого состояния'. ``progress`` is the prior
    worker turn's output; ``next_step`` is the judge's reason."""
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    occ = iter([0.75, 0.15])
    spec_dir = tmp_path / "handoff"

    goals.run_kanban_goal_loop(
        task_id="round-trip", goal_text="THE GOAL TEXT",
        run_turn=lambda p: "next output",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="FIRST-TURN-OUTPUT",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=lambda: next(occ),
        reset_session_fn=lambda: None,
        session_id_fn=lambda: "sess-XYZ",
        spec_dir=spec_dir,
    )

    # Exactly one spec file, at the deterministic per-(task, index) path.
    expected_path = sh.spec_path(spec_dir, "round-trip", 1)
    assert expected_path.exists()

    loaded = json.loads(expected_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == sh.SPEC_SCHEMA_VERSION
    assert loaded["kind"] == sh.SPEC_KIND
    assert loaded["task_id"] == "round-trip"
    assert loaded["goal"] == "THE GOAL TEXT"
    # progress == the response from the turn BEFORE the handoff was decided.
    assert loaded["progress"] == "FIRST-TURN-OUTPUT"
    # next_step == the judge's reason that drove the continuation.
    assert loaded["next_step"] == "scripted:continue"
    assert loaded["handoff_index"] == 1
    assert loaded["source_session_id"] == "sess-XYZ"
    assert loaded["occupancy_at_handoff"] == 0.75
    assert loaded["created_at"]  # non-empty ISO timestamp

    # The loaded state fully re-hydrates the fresh session's continuation prompt.
    prompt = sh.render_continuation_prompt(expected_path, loaded)
    assert "THE GOAL TEXT" in prompt
    assert "FIRST-TURN-OUTPUT" in prompt
    assert str(expected_path) in prompt


# ---------------------------------------------------------------------------
# 5. Hermes context metrics unavailable -> continue in-session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("occ_fn", [
    lambda: None,                                   # metric simply absent
    lambda: (_ for _ in ()).throw(RuntimeError()),  # metric probe raises
], ids=["returns_none", "raises"])
def test_metrics_unavailable_continues_without_handoff(monkeypatch, tmp_path, occ_fn):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    resets = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="nometrics", goal_text="ship",
        run_turn=lambda p: turns.append(p) or "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="started",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=occ_fn,
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    # Unknown occupancy never hands off; the loop just runs the next turn.
    assert resets == []
    assert all(HANDOFF_MARKER not in p for p in turns)


# ---------------------------------------------------------------------------
# 6. Hard turn-budget enforcement is unchanged while soft handoff is active
# ---------------------------------------------------------------------------

def test_hard_turn_budget_still_enforced_with_handoff_active(monkeypatch, tmp_path):
    """Occupancy is pinned high and the handoff cap is set huge, so the soft
    layer hands off every turn - yet the hard turn budget must still be the last
    line and block the card. Proves no regression in prior enforcement."""
    _patch_judge(monkeypatch, ["continue"] * 20)
    resets = []
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="hardlimit", goal_text="endless",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=3, first_response="turn1",
        soft_handoff_config=_cfg(max_handoffs=1000),  # cap never the limiter
        context_occupancy_fn=lambda: 0.95,
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert res["reason"] == "turn budget exhausted"
    # The card was blocked for review with the budget message (not a silent exit).
    assert "turn budget" in blocked.get("reason", "")
    # Handoffs did fire (cap wasn't hit), proving the two layers coexist.
    assert len(resets) >= 1


def test_soft_handoff_never_swallows_worker_block(monkeypatch, tmp_path):
    """If the worker itself blocks the card mid-run, that terminal state wins -
    the soft layer must not mask it. (Regression guard on enforcement.)"""
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "blocked"])
    resets = []

    res = goals.run_kanban_goal_loop(
        task_id="workerblock", goal_text="ship",
        run_turn=lambda p: "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("dispatcher should not re-block"),
        max_turns=10, first_response="started",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=lambda: 0.99,
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "blocked_by_worker"


# ---------------------------------------------------------------------------
# 7. Exact call ORDER inside a handoff (deterministic sequence)
# ---------------------------------------------------------------------------

def test_handoff_call_sequence_is_ordered(monkeypatch, tmp_path):
    """Assert the precise ordering the policy promises: occupancy is read, the
    spec is written to disk, and ONLY THEN is the session reset, before the
    next turn runs on the fresh session. The reset callback checks the spec
    already exists on disk at reset time - proving write-before-reset."""
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    occ_values = iter([0.85, 0.10])
    spec_dir = tmp_path / "handoff"
    expected_spec = sh.spec_path(spec_dir, "seq", 1)
    events = []

    def _occ():
        v = next(occ_values)
        events.append(f"read_occ:{v}")
        return v

    def _reset():
        # Spec MUST already be on disk before we tear down the session.
        events.append("reset:spec_present" if expected_spec.exists() else "reset:spec_absent")

    def _run_turn(prompt):
        events.append("turn:handoff" if HANDOFF_MARKER in prompt else "turn:normal")
        return "work"

    res = goals.run_kanban_goal_loop(
        task_id="seq", goal_text="ship",
        run_turn=_run_turn,
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="started",
        soft_handoff_config=_cfg(),
        context_occupancy_fn=_occ,
        reset_session_fn=_reset,
        session_id_fn=lambda: "sess0",
        spec_dir=spec_dir,
    )
    assert res["outcome"] == "completed_by_worker"
    # Turn 1: read 0.85 -> write spec -> reset (spec present) -> handoff turn.
    # Turn 2: read 0.10 -> below threshold, no reset -> normal turn.
    assert events == [
        "read_occ:0.85",
        "reset:spec_present",
        "turn:handoff",
        "read_occ:0.1",
        "turn:normal",
    ]
