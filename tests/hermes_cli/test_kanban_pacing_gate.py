"""Per-pocket dispatch gating — hard block + graceful admission (task t_4ee09bd0).

Regression from the pacing actuation split (t_dcd5b570): the split stopped the
hot Claude windows from imposing a board-wide cap that starved non-Claude
workers, but in doing so a pocket's OWN window stopped throttling ITS OWN
dispatches — glm could sit at 96% of its 5h session and keep spawning glm work.

This card's refinement (t_4ee09bd0) replaces the BINARY "tripped -> block all"
rule with GRACEFUL ADMISSION:

- A pocket that is HARD-BLOCKED (provider cooldown or near-exhaustion >= 95%)
  still skips ALL its dispatches — admitting any task would blow a real limit.
- A pocket that is merely OVERSHOOTING its sustainable rate (ratio < 1.0) is
  rate-capped: its in-flight worker count is held to a fraction of the board's
  concurrency so spend paces down smoothly instead of slamming to zero. It
  keeps a trickle (floored to 1) and never self-locks.
- A pocket that has since gone IDLE (recent rate ~0) is NOT tripped by the
  stale lifetime-average projection — a day-one onboarding burst can't poison
  the whole week.

So: glm throttled + kimi free -> glm waits, kimi spawns; glm overshooting +
kimi free -> glm trickles (cap), kimi spawns freely.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


# --- pure fold logic: zeus_pacing.pocket_admission / throttled_pockets --------

def _pocket(subscription, *, state, reason, window_label="Current week",
            hard_block=False, sustainable_rate_ratio=None,
            recent_rate_per_sec=None):
    """A minimal pacing-snapshot pocket entry carrying an effective breaker.

    ``hard_block`` and ``sustainable_rate_ratio`` are the graceful-admission
    signals the folded effective breaker now carries (mirroring what
    :func:`zeus_circuit_breaker.aggregate` produces from per-window verdicts).
    """
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
            "hard_block": hard_block,
            "sustainable_rate_ratio": sustainable_rate_ratio,
            "recent_rate_per_sec": recent_rate_per_sec,
            "reason": reason,
        },
    }


def test_throttled_pockets_is_hard_block_subset(monkeypatch):
    """throttled_pockets() returns ONLY the hard-blocked pockets — projection-
    only overshoot is paced gracefully via pocket_admission(), not skipped."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "board": "ra",
        "now": 0.0,
        "pockets": [
            # glm: near-exhaustion hard-block -> skipped entirely
            _pocket("glm", state="open", reason="spent 96% of budget",
                    window_label="Current session", hard_block=True),
            _pocket("glm", state="closed", reason="on pace",
                    window_label="Current week", hard_block=True),
            # kimi: overshooting but NOT a hard block -> graceful cap, not skip
            _pocket("kimi", state="half_open",
                    reason="projected 110% of budget by reset",
                    window_label="Current week",
                    hard_block=False, sustainable_rate_ratio=0.5),
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)

    throttled = zeus_pacing.throttled_pockets(object(), "ra", now=0.0)
    assert throttled == {"glm": "spent 96% of budget"}
    assert "kimi" not in throttled  # graceful, not skipped


def test_pocket_admission_carries_graceful_verdict(monkeypatch):
    """pocket_admission() returns the full verdict: hard_block pockets + the
    sustainable_rate_ratio for gracefully-capped ones."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "board": "ra",
        "now": 0.0,
        "pockets": [
            _pocket("glm", state="open", reason="spent 96% of budget",
                    window_label="Current session", hard_block=True),
            _pocket("kimi", state="half_open",
                    reason="projected 110% of budget by reset",
                    window_label="Current week",
                    hard_block=False, sustainable_rate_ratio=0.5,
                    recent_rate_per_sec=12.0),
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)

    admission = zeus_pacing.pocket_admission(object(), "ra", now=0.0)
    assert admission["glm"]["hard_block"] is True
    assert admission["glm"]["sustainable_rate_ratio"] is None
    assert admission["kimi"]["hard_block"] is False
    assert admission["kimi"]["sustainable_rate_ratio"] == 0.5
    assert admission["kimi"]["recent_rate_per_sec"] == 12.0


def test_throttled_pockets_open_wins_over_burndown(monkeypatch):
    """Aggregate doctrine: a hard wall in the weekly window trips the pocket even
    when the nested session is burning down (open outranks burndown)."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "pockets": [
            _pocket("kimi", state="burndown", reason="burning down",
                    window_label="Current session", hard_block=False),
            _pocket("kimi", state="open", reason="spent 97% of budget",
                    window_label="Current week", hard_block=True),
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)

    out = zeus_pacing.throttled_pockets(object(), "ra", now=0.0)
    assert out == {"kimi": "spent 97% of budget"}


def test_pocket_admission_fail_open_on_error(monkeypatch):
    """A pacing-read error degrades to {} — pacing never stalls the board."""
    from hermes_cli import zeus_pacing

    def _boom(*a, **k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", _boom)
    assert zeus_pacing.pocket_admission(object(), "ra", now=0.0) == {}


def test_pocket_admission_for_board_no_ledger(monkeypatch):
    """No zeus ledger (plugin never ran) -> {} (fail open)."""
    from hermes_cli import zeus_pacing

    monkeypatch.setattr(zeus_pacing, "connect", lambda *a, **k: None)
    assert zeus_pacing.pocket_admission_for_board("ra") == {}


def test_throttled_pockets_for_board_no_ledger(monkeypatch):
    """No zeus ledger (plugin never ran) -> {} (fail open)."""
    from hermes_cli import zeus_pacing

    monkeypatch.setattr(zeus_pacing, "connect", lambda *a, **k: None)
    assert zeus_pacing.throttled_pockets_for_board("ra") == {}


def test_tracking_only_pocket_never_gates(monkeypatch):
    """A pocket with pacing_config.enabled=False is observed but never gates
    dispatch (tracking-only) — the self-lock fix from 35d636f, preserved."""
    from hermes_cli import zeus_pacing

    fake_snapshot = {
        "pockets": [
            {"subscription": "kimi", "enabled": False,
             "effective_breaker": {"state": "open", "tripped": True,
                                   "hard_block": True,
                                   "sustainable_rate_ratio": None,
                                   "reason": "would self-lock"}},
        ],
    }
    monkeypatch.setattr(zeus_pacing, "pacing_snapshot", lambda *a, **k: fake_snapshot)
    assert zeus_pacing.pocket_admission(object(), "ra", now=0.0) == {}
    assert zeus_pacing.throttled_pockets(object(), "ra", now=0.0) == {}


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


def _admission(pocket, *, hard_block=False, ratio=None, reason="throttled",
               state="half_open"):
    """Build a resolve_pocket_admission-style verdict for one pocket."""
    return {
        pocket: {
            "hard_block": hard_block,
            "sustainable_rate_ratio": ratio,
            "recent_rate_per_sec": None,
            "reason": reason,
            "state": state,
        }
    }


def test_glm_hard_blocked_kimi_free(isolated_kanban_home_with_profiles, monkeypatch):
    """The headline scenario: glm's pocket is HARD-BLOCKED, kimi's is free.
    glm-assigned tasks defer to skipped_pacing_throttled; kimi tasks spawn."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission(
            "glm", hard_block=True, reason="spent 96% of budget", state="open"
        ),
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
    # Graceful admission list stays empty — these were hard blocks.
    assert res.skipped_pacing_admission == []


def test_no_throttle_all_spawn(isolated_kanban_home_with_profiles, monkeypatch):
    """Baseline: no pocket throttled -> nothing gated, everything spawns."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(kb, "resolve_pocket_admission", lambda board=None: {})
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="glm0", assignee="glm")
        kb.create_task(conn, title="kimi0", assignee="kimi")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert len(res.spawned) == 2
    assert not res.skipped_pacing_throttled
    assert not res.skipped_pacing_admission


def test_hard_block_gate_is_case_insensitive(isolated_kanban_home_with_profiles, monkeypatch):
    """A pocket name resolved to lower-case still matches a differently-cased
    assignee (resolve_pocket_admission already lower-cases its keys)."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission("glm", hard_block=True, reason="throttled"),
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="GLM", assignee="GLM")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert not res.spawned
    assert len(res.skipped_pacing_throttled) == 1


def test_hard_block_emits_event_and_leaves_task_ready(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """A real (non-dry) hard-blocked tick emits a ``pacing_throttled`` event and
    leaves the task in ``ready`` (not claimed / not failed) for a later tick."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission(
            "glm", hard_block=True, reason="spent 96% of budget", state="open"
        ),
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


def test_hard_blocked_task_dispatches_after_cooldown(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """The gate is per-tick state: once the pocket's window cools, the same task
    dispatches on a subsequent tick (not a permanent block)."""
    kb = isolated_kanban_home_with_profiles
    state = {"adm": _admission("glm", hard_block=True, reason="spent 96% of budget",
                               state="open")}
    monkeypatch.setattr(kb, "resolve_pocket_admission", lambda board=None: state["adm"])
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="glm0", assignee="glm")

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert not res1.spawned
    assert len(res1.skipped_pacing_throttled) == 1

    # Window cooled -> pocket no longer throttled.
    state["adm"] = {}
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert [s[0] for s in res2.spawned] == [tid]
    assert not res2.skipped_pacing_throttled


# --- graceful admission (the new behavior) -----------------------------------

def test_graceful_admission_caps_overshooting_pocket(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """A pocket overshooting its sustainable rate (ratio 0.5) but NOT hard-
    blocked is rate-capped, not skipped. With the pocket already at its cap of
    in-flight workers, new tasks defer to skipped_pacing_admission (not
    skipped_pacing_throttled) and a free pocket keeps spawning."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission(
            "glm", hard_block=False, ratio=0.5,
            reason="projected 140% of budget by reset", state="half_open",
        ),
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # Two glm tasks already running -> at the cap (round(10*0.5)=5, but with
        # max_in_progress=4 the cap is round(4*0.5)=2). Use a small board.
        for i in range(2):
            t = kb.create_task(conn, title=f"running{i}", assignee="glm")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
        # Two more glm tasks ready -> should be admission-capped, not spawned.
        for i in range(2):
            kb.create_task(conn, title=f"ready{i}", assignee="glm")
        # A kimi task ready -> free pocket, spawns normally.
        kb.create_task(conn, title="kimi0", assignee="kimi")
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True,
                               max_in_progress=4)
    spawned = [s[1] for s in res.spawned]
    # kimi spawns (free pocket); glm ready tasks are admission-capped.
    assert spawned.count("kimi") == 1
    assert spawned.count("glm") == 0
    assert len(res.skipped_pacing_admission) == 2
    assert all(t[1] == "glm" for t in res.skipped_pacing_admission)
    # Hard-block list stays empty — this was graceful, not a skip-all.
    assert res.skipped_pacing_throttled == []


def test_graceful_admission_lets_first_task_through(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """Graceful admission floors to 1: an overshooting pocket with NOTHING in
    flight still admits one task (trickle), so it never self-locks."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission(
            "glm", hard_block=False, ratio=0.2, reason="overshooting",
            state="half_open",
        ),
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="glm0", assignee="glm")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True,
                               max_in_progress=4)
    # cap = max(1, round(4*0.2)) = max(1, 1) = 1; in-flight 0 < 1 -> admit.
    assert len(res.spawned) == 1
    assert res.skipped_pacing_admission == []


def test_graceful_admission_emits_event_and_leaves_task_ready(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """A real (non-dry) admission-capped tick emits a ``pacing_admission`` event
    and leaves the task in ``ready`` for a later tick."""
    kb = isolated_kanban_home_with_profiles
    monkeypatch.setattr(
        kb, "resolve_pocket_admission",
        lambda board=None: _admission(
            "glm", hard_block=False, ratio=0.5, reason="overshooting",
            state="half_open",
        ),
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # Fill the cap with running tasks.
        for i in range(2):
            t = kb.create_task(conn, title=f"running{i}", assignee="glm")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
        tid = kb.create_task(conn, title="ready", assignee="glm")
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False,
                               max_in_progress=4)
        assert not res.spawned
        assert [t[0] for t in res.skipped_pacing_admission] == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.claim_lock is None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "pacing_admission" in kinds


def test_graceful_admission_clears_when_pace_restored(
    isolated_kanban_home_with_profiles, monkeypatch
):
    """Once the pocket's rate drops back under the sustainable line (ratio
    becomes None — on/under pace), admission stops capping and the task spawns."""
    kb = isolated_kanban_home_with_profiles
    state = {"adm": _admission("glm", hard_block=False, ratio=0.5,
                               reason="overshooting", state="half_open")}
    monkeypatch.setattr(kb, "resolve_pocket_admission", lambda board=None: state["adm"])
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(2):
            t = kb.create_task(conn, title=f"running{i}", assignee="glm")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
        tid = kb.create_task(conn, title="ready", assignee="glm")
        conn.commit()
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False,
                                max_in_progress=4)
    assert not res1.spawned
    assert len(res1.skipped_pacing_admission) == 1

    # Pace restored -> no ratio -> no cap.
    state["adm"] = _admission("glm", hard_block=False, ratio=None,
                              reason="on pace", state="closed")
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False,
                                max_in_progress=4)
    assert [s[0] for s in res2.spawned] == [tid]
    assert not res2.skipped_pacing_admission


def test_dispatch_result_has_pacing_fields():
    """Schema invariant: DispatchResult exposes both pacing lists."""
    from hermes_cli.kanban_db import DispatchResult

    r = DispatchResult()
    assert hasattr(r, "skipped_pacing_throttled")
    assert hasattr(r, "skipped_pacing_admission")
    assert r.skipped_pacing_throttled == []
    assert r.skipped_pacing_admission == []
