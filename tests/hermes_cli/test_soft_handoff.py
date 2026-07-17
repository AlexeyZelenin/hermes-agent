"""Tests for the soft context-budget handoff policy and its goal-loop wiring.

Two layers:

1. Policy (``hermes_cli.soft_handoff``): threshold evaluation, config parsing,
   versioned spec build/write (idempotent), continuation-prompt rendering, and
   the ``maybe_handoff`` orchestrator with all its fallback paths.
2. Goal loop integration: ``goals.run_kanban_goal_loop`` performs the handoff
   exactly once near the threshold, resumes in a fresh session, respects the
   cyclic-handoff cap, and stays inert when disabled.

Everything is driven through injected callbacks and tmp dirs - no live model
and no live kanban board.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import soft_handoff as sh


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults():
    cfg = sh.SoftHandoffConfig()
    assert cfg.enabled is True
    assert cfg.soft_pct == 0.60
    assert cfg.max_handoffs == 2
    assert cfg.schema_version == sh.SPEC_SCHEMA_VERSION


def test_config_from_env_parsing():
    cfg = sh.SoftHandoffConfig.from_env(
        {"HERMES_KANBAN_SOFT_HANDOFF": "0", "HERMES_KANBAN_SOFT_HANDOFF_PCT": "75",
         "HERMES_KANBAN_SOFT_HANDOFF_MAX": "5"}
    )
    assert cfg.enabled is False
    assert cfg.soft_pct == 0.75  # "75" -> 0.75
    assert cfg.max_handoffs == 5


def test_config_from_env_fraction_and_bad_values():
    cfg = sh.SoftHandoffConfig.from_env({"HERMES_KANBAN_SOFT_HANDOFF_PCT": "0.5"})
    assert cfg.soft_pct == 0.5
    # Out-of-range / unparseable -> default.
    from_env = sh.SoftHandoffConfig.from_env
    assert from_env({"HERMES_KANBAN_SOFT_HANDOFF_PCT": "0"}).soft_pct == 0.60
    assert from_env({"HERMES_KANBAN_SOFT_HANDOFF_PCT": "nope"}).soft_pct == 0.60


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------

CFG = sh.SoftHandoffConfig(enabled=True, soft_pct=0.60, max_handoffs=2)


def test_evaluate_handoff_when_over_threshold():
    d = sh.evaluate(0.62, compaction_active=False, handoffs_done=0, config=CFG)
    assert d.should_handoff
    assert d.code == sh.CODE_THRESHOLD_REACHED


@pytest.mark.parametrize("occ,compaction,done,code", [
    (0.50, False, 0, sh.CODE_BELOW_THRESHOLD),
    (None, False, 0, sh.CODE_NO_METRICS),
    (0.90, True, 0, sh.CODE_COMPACTING),
    (0.90, False, 2, sh.CODE_HANDOFF_CAP),
])
def test_evaluate_continue_paths(occ, compaction, done, code):
    d = sh.evaluate(occ, compaction_active=compaction, handoffs_done=done, config=CFG)
    assert not d.should_handoff
    assert d.code == code


def test_evaluate_disabled():
    d = sh.evaluate(0.99, compaction_active=False, handoffs_done=0,
                    config=sh.SoftHandoffConfig(enabled=False))
    assert not d.should_handoff
    assert d.code == sh.CODE_DISABLED


# ---------------------------------------------------------------------------
# spec build / write
# ---------------------------------------------------------------------------

def test_build_spec_is_versioned_contract():
    spec = sh.build_spec(
        task_id="t1", goal_text="ship it", progress="did A and B",
        next_step="do C", handoff_index=1, source_session_id="s0",
        occupancy=0.61, created_at="2026-07-17T00:00:00+00:00",
        decisions=["chose X over Y"],
    )
    assert spec["schema_version"] == 1
    assert spec["kind"] == sh.SPEC_KIND
    assert spec["task_id"] == "t1"
    assert spec["goal"] == "ship it"
    assert spec["progress"] == "did A and B"
    assert spec["next_step"] == "do C"
    assert spec["decisions"] == ["chose X over Y"]
    assert spec["handoff_index"] == 1
    assert spec["source_session_id"] == "s0"
    assert spec["occupancy_at_handoff"] == 0.61


def test_write_spec_file_idempotent(tmp_path):
    spec = sh.build_spec(
        task_id="t1", goal_text="g", progress="p", next_step="n",
        handoff_index=1, source_session_id="s", occupancy=0.6, created_at="ts",
    )
    path = sh.spec_path(tmp_path, "t1", 1)
    assert sh.write_spec_file(path, spec) is True
    assert path.exists()
    first_mtime = path.stat().st_mtime_ns
    # Writing identical content again is a no-op (not rewritten).
    assert sh.write_spec_file(path, spec) is True
    assert path.stat().st_mtime_ns == first_mtime
    loaded = json.loads(path.read_text())
    assert loaded["task_id"] == "t1"


def test_write_spec_file_failure_returns_false(tmp_path):
    # Make the parent path a FILE so mkdir/replace fails -> False, not a raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    path = blocker / "sub" / "spec.json"
    assert sh.write_spec_file(path, {"a": 1}) is False


def test_render_continuation_prompt_embeds_state(tmp_path):
    spec = sh.build_spec(
        task_id="t1", goal_text="THE GOAL", progress="PROGRESS TEXT",
        next_step="NEXT THING", handoff_index=1, source_session_id="s",
        occupancy=0.6, created_at="ts", decisions=["decided D"],
    )
    path = sh.spec_path(tmp_path, "t1", 1)
    prompt = sh.render_continuation_prompt(path, spec)
    assert "THE GOAL" in prompt
    assert "PROGRESS TEXT" in prompt
    assert "NEXT THING" in prompt
    assert "decided D" in prompt
    assert str(path) in prompt


# ---------------------------------------------------------------------------
# maybe_handoff() orchestrator
# ---------------------------------------------------------------------------

def test_maybe_handoff_performs_handoff(tmp_path):
    resets = []
    out = sh.maybe_handoff(
        base_prompt="normal continue", task_id="t1", goal_text="g",
        progress="prog", next_step="next", handoffs_done=0, config=CFG,
        occupancy_fn=lambda: 0.7, reset_fn=lambda: resets.append(True),
        session_id_fn=lambda: "sess0", spec_dir=tmp_path, now_fn=lambda: "ts",
    )
    assert out.handed_off is True
    assert out.prompt != "normal continue"
    assert "SOFT CONTEXT HANDOFF" in out.prompt
    assert resets == [True]
    assert Path(out.spec_path).exists()


def test_maybe_handoff_no_metrics_is_fallback(tmp_path):
    resets = []
    out = sh.maybe_handoff(
        base_prompt="base", task_id="t1", goal_text="g", progress="p",
        next_step="n", handoffs_done=0, config=CFG,
        occupancy_fn=lambda: None, reset_fn=lambda: resets.append(True),
        spec_dir=tmp_path,
    )
    assert out.handed_off is False
    assert out.prompt == "base"
    assert resets == []


def test_maybe_handoff_reset_failure_is_fallback(tmp_path):
    def _boom():
        raise RuntimeError("cannot reset")

    out = sh.maybe_handoff(
        base_prompt="base", task_id="t1", goal_text="g", progress="p",
        next_step="n", handoffs_done=0, config=CFG,
        occupancy_fn=lambda: 0.9, reset_fn=_boom, spec_dir=tmp_path,
        now_fn=lambda: "ts",
    )
    assert out.handed_off is False
    assert out.prompt == "base"


def test_maybe_handoff_spec_write_failure_is_fallback(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    resets = []
    out = sh.maybe_handoff(
        base_prompt="base", task_id="t1", goal_text="g", progress="p",
        next_step="n", handoffs_done=0, config=CFG,
        occupancy_fn=lambda: 0.9, reset_fn=lambda: resets.append(True),
        spec_dir=blocker / "sub", now_fn=lambda: "ts",
    )
    assert out.handed_off is False
    assert out.prompt == "base"
    assert resets == []  # reset is never reached if the spec cannot be saved


# ---------------------------------------------------------------------------
# Goal-loop integration
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        return v, f"scripted:{v}", False, None

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_goal_loop_hands_off_once_near_threshold(monkeypatch, tmp_path):
    _patch_judge(monkeypatch, ["continue", "continue"])
    # running through the first two status checks, then worker completes.
    statuses = iter(["running", "running", "done"])
    # occupancy crosses the threshold on the first continuation, then a fresh
    # session drops it back down.
    occ = iter([0.72, 0.20, 0.20])
    resets = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1", goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or "did more work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="started",
        soft_handoff_config=sh.SoftHandoffConfig(enabled=True, soft_pct=0.60, max_handoffs=2),
        context_occupancy_fn=lambda: next(occ),
        reset_session_fn=lambda: resets.append(True),
        session_id_fn=lambda: "sess0",
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    # Exactly one handoff: reset called once, and the first continuation turn
    # got the handoff prompt (fresh session), the second a normal one.
    assert resets == [True]
    assert "SOFT CONTEXT HANDOFF" in turns[0]
    assert "SOFT CONTEXT HANDOFF" not in turns[1]
    # Spec file was written to the isolated dir.
    specs = list((tmp_path / "handoff").glob("*.json"))
    assert len(specs) == 1


def test_goal_loop_respects_handoff_cap(monkeypatch, tmp_path):
    _patch_judge(monkeypatch, ["continue"] * 10)
    # Occupancy stays high the whole time; cap is 1, so only one handoff.
    resets = []
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t2", goal_text="endless",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=4, first_response="turn1",
        soft_handoff_config=sh.SoftHandoffConfig(enabled=True, soft_pct=0.60, max_handoffs=1),
        context_occupancy_fn=lambda: 0.95,
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    # Turn budget (hard defense) still bounds the loop.
    assert res["outcome"] == "blocked_budget"
    # Cap honoured: at most one handoff despite persistent high occupancy.
    assert len(resets) == 1


def test_goal_loop_disabled_never_hands_off(monkeypatch, tmp_path):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "done"])
    resets = []

    res = goals.run_kanban_goal_loop(
        task_id="t3", goal_text="task",
        run_turn=lambda p: "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="start",
        soft_handoff_config=sh.SoftHandoffConfig(enabled=False),
        context_occupancy_fn=lambda: 0.99,
        reset_session_fn=lambda: resets.append(True),
        spec_dir=tmp_path / "handoff",
    )
    assert res["outcome"] == "completed_by_worker"
    assert resets == []


def test_goal_loop_no_config_is_unchanged(monkeypatch):
    # Backward compatibility: omitting soft-handoff args behaves exactly as before.
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t4", goal_text="ship",
        run_turn=lambda p: turns.append(p) or "work",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10, first_response="start",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 2
    assert all("SOFT CONTEXT HANDOFF" not in p for p in turns)
