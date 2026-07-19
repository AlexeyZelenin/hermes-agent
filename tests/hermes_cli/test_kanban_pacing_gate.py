"""Per-pocket dispatch gating (task t_4ee09bd0).

Regression from the pacing actuation split (t_dcd5b570): the split stopped the
hot Claude windows from imposing a board-wide cap that starved non-Claude
workers, but in doing so a pocket's OWN window stopped throttling ITS OWN
dispatches — glm could sit at 96% of its 5h session and keep spawning glm work.

The fix: the dispatcher resolves per-pocket throttle verdicts and defers a ready
task whose assignee names a throttled pocket, while a FREE pocket (a different
subscription) keeps dispatching. glm throttled + kimi free -> glm waits, kimi
spawns.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


# --- pure fold logic: zeus_pacing.throttled_pockets --------------------------

def _pocket(subscription, *, state, reason, window_label="Current week"):
    """A minimal pacing-snapshot pocket entry carrying an effective breaker."""
    tripped = state == "open"
    limit = {"open": 0, "half_open": 1}.get(state)
    return {
        "subscription": subscription,
        "window_label": window_label,
        "effective_breaker": {
            "state": state,
            "tripped": tripped,
            "throttling": state == "half_open",
            "burning_down": state == "burndown",
            "recommended_agent_limit": limit,
            "reason": reason,
        },
    }


def test_throttled_pockets_folds_windows_and_filters_free(monkeypatch):
    """A pocket tripped in ANY of its windows is returned; a free pocket is not."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "board": "ra",
        "now": 0.0,
        "pockets": [
            # glm: session window tripped -> pocket throttled
            _pocket("glm", state="open", reason="spent 96% of budget",
                    window_label="Current session"),
            _pocket("glm", state="closed", reason="on pace",
                    window_label="Current week"),
            # kimi: both windows healthy -> free
            _pocket("kimi", state="closed", reason="on pace",
                    window_label="Current session"),
            _pocket("kimi", state="burndown", reason="burning down",
                    window_label="Current week"),
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)

    out = zeus_pacing.throttled_pockets(object(), "ra", now=0.0)
    assert out == {"glm": "spent 96% of budget"}
    assert "kimi" not in out


def test_throttled_pockets_open_wins_over_burndown(monkeypatch):
    """Aggregate doctrine: a hard wall in the weekly window trips the pocket even
    when the nested session is burning down (open outranks burndown)."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "pockets": [
            _pocket("kimi", state="burndown", reason="burning down",
                    window_label="Current session"),
            _pocket("kimi", state="open", reason="projected 147% of budget by reset",
                    window_label="Current week"),
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)

    out = zeus_pacing.throttled_pockets(object(), "ra", now=0.0)
    assert out == {"kimi": "projected 147% of budget by reset"}


def test_throttled_pockets_fail_open_on_error(monkeypatch):
    """A pacing-read error degrades to {} — pacing never stalls the board."""
    from hermes_cli import zeus_pacing

    def _boom(*a, **k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", _boom)
    assert zeus_pacing.throttled_pockets(object(), "ra", now=0.0) == {}


def test_throttled_pockets_for_board_no_ledger(monkeypatch):
    """No zeus ledger (plugin never ran) -> {} (fail open)."""
    from hermes_cli import zeus_pacing

    monkeypatch.setattr(zeus_pacing, "connect", lambda *a, **k: None)
    assert zeus_pacing.throttled_pockets_for_board("ra") == {}


# --- dispatcher integration --------------------------------------------------

@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB + glm/kimi/default profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_pacing_gate_test_")
    for prof in ("glm", "kimi", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 4242


def test_glm_throttled_kimi_free(isolated_kanban_home_with_profiles, monkeypatch):
    """The headline scenario: glm's pocket is throttled, kimi's is free.
    glm-assigned tasks defer to skipped_pacing_throttled; kimi tasks spawn."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_throttled_pockets",
        lambda board=None: {"glm": "spent 96% of budget"},
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"glm{i}", assignee="glm")
        for i in range(2):
            kb.create_task(conn, title=f"kimi{i}", assignee="kimi")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    spawned = [s[1] for s in res.spawned]
    throttled = [(t[1], t[2]) for t in res.skipped_pacing_throttled]
    assert spawned.count("kimi") == 2
    assert spawned.count("glm") == 0
    assert len(throttled) == 3
    assert all(a == "glm" and r == "spent 96% of budget" for a, r in throttled)


def test_no_throttle_all_spawn(isolated_kanban_home_with_profiles, monkeypatch):
    """Baseline: no pocket throttled -> nothing gated, everything spawns."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(kb, "resolve_throttled_pockets", lambda board=None: {})
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="glm0", assignee="glm")
        kb.create_task(conn, title="kimi0", assignee="kimi")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert len(res.spawned) == 2
    assert not res.skipped_pacing_throttled


def test_throttle_gate_is_case_insensitive(isolated_kanban_home_with_profiles, monkeypatch):
    """A pocket name resolved to lower-case still matches a differently-cased
    assignee (resolve_throttled_pockets already lower-cases its keys)."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_throttled_pockets", lambda board=None: {"glm": "throttled"},
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="GLM", assignee="GLM")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert not res.spawned
    assert len(res.skipped_pacing_throttled) == 1


def test_throttle_emits_event_and_leaves_task_ready(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """A real (non-dry) throttled tick emits a ``pacing_throttled`` event and
    leaves the task in ``ready`` (not claimed / not failed) for a later tick."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_throttled_pockets",
        lambda board=None: {"glm": "spent 96% of budget"},
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="glm0", assignee="glm")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        assert not res.spawned
        assert [t[0] for t in res.skipped_pacing_throttled] == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.claim_lock is None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "pacing_throttled" in kinds


def test_throttled_task_dispatches_after_cooldown(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """The gate is per-tick state: once the pocket's window cools, the same task
    dispatches on a subsequent tick (not a permanent block)."""
    kb = isolated_kanban_home_with_profiles
    state = {"throttled": {"glm": "spent 96% of budget"}}
    monkeypatch.setattr(kb, "resolve_throttled_pockets", lambda board=None: state["throttled"])
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="glm0", assignee="glm")

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert not res1.spawned
    assert len(res1.skipped_pacing_throttled) == 1

    # Window cooled -> pocket no longer throttled.
    state["throttled"] = {}
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert [s[0] for s in res2.spawned] == [tid]
    assert not res2.skipped_pacing_throttled


def test_dispatch_result_has_skipped_pacing_throttled_field():
    """Schema invariant: DispatchResult exposes skipped_pacing_throttled as a
    list of (task_id, assignee, reason) tuples."""
    from hermes_cli.kanban_db import DispatchResult

    r = DispatchResult()
    assert hasattr(r, "skipped_pacing_throttled")
    assert r.skipped_pacing_throttled == []
