"""Tests for the self-redeploy policy controller (task t_42950fee).

Companion to ``tests/test_code_skew.py``: that proves the boot-vs-disk drift
signal; these prove the policy that turns it into a notify / safe-restart
action, with debounce and the never-interrupt-work quiet gate.
"""

import logging

from gateway import self_redeploy as sr
from gateway.self_redeploy import RedeployController


def _controller(*, skew, active=0):
    """Build a controller with stub callables. Returns (controller, calls).

    ``skew`` may be a fixed value or a zero-arg callable (so a test can change
    the drift between ticks). ``active`` is the in-flight-work count.
    """
    calls = {"restart": 0}
    skew_fn = skew if callable(skew) else (lambda: skew)
    ctrl = RedeployController(
        detect_skew=skew_fn,
        count_active_work=lambda: active,
        request_restart=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        logger=logging.getLogger("test.self_redeploy"),
    )
    return ctrl, calls


class TestNormalizeMode:
    def test_valid_modes_pass_through(self):
        assert sr.normalize_redeploy_mode("off") == "off"
        assert sr.normalize_redeploy_mode("notify") == "notify"
        assert sr.normalize_redeploy_mode("safe-restart") == "safe-restart"

    def test_case_and_whitespace_normalized(self):
        assert sr.normalize_redeploy_mode("  Safe-Restart  ") == "safe-restart"

    def test_unknown_falls_back_to_notify_never_restart(self):
        # A typo must never escalate to safe-restart.
        assert sr.normalize_redeploy_mode("safe_restart") == "notify"
        assert sr.normalize_redeploy_mode("restart") == "notify"
        assert sr.normalize_redeploy_mode("") == "notify"
        assert sr.normalize_redeploy_mode(None) == "notify"


class TestResolveMode:
    def test_reads_kanban_auto_redeploy(self):
        cfg = {"kanban": {"auto_redeploy": "safe-restart"}}
        assert sr.resolve_redeploy_mode(lambda: cfg) == "safe-restart"

    def test_missing_key_defaults_notify(self):
        assert sr.resolve_redeploy_mode(lambda: {"kanban": {}}) == "notify"

    def test_config_read_failure_defaults_notify(self):
        def _boom():
            raise RuntimeError("config unreadable")

        assert sr.resolve_redeploy_mode(_boom) == "notify"


class TestNoSkew:
    def test_no_drift_is_idle(self):
        ctrl, calls = _controller(skew=None)
        d = ctrl.evaluate("safe-restart")
        assert d.outcome == sr.IDLE
        assert not d.first_time
        assert calls["restart"] == 0

    def test_resolved_drift_clears_debounce_so_redrift_renotifies(self):
        # Notify once, drift reverts (no skew), then a fresh drift re-notifies.
        drift = {"val": ("boot0", "diskA")}
        ctrl, _ = _controller(skew=lambda: drift["val"])
        assert ctrl.evaluate("notify").outcome == sr.NOTIFIED
        drift["val"] = None
        assert ctrl.evaluate("notify").outcome == sr.IDLE
        drift["val"] = ("boot0", "diskA")  # same rev returns after a clean tick
        assert ctrl.evaluate("notify").first_time is True


class TestOffMode:
    def test_off_never_acts_even_with_drift(self):
        ctrl, calls = _controller(skew=("boot0", "diskA"), active=0)
        d = ctrl.evaluate("off")
        assert d.outcome == sr.IDLE
        assert not d.first_time
        assert calls["restart"] == 0


class TestNotifyMode:
    def test_first_drift_notifies(self):
        ctrl, calls = _controller(skew=("boot0", "diskA"))
        d = ctrl.evaluate("notify")
        assert d.outcome == sr.NOTIFIED
        assert d.first_time
        assert (d.boot_rev, d.disk_rev) == ("boot0", "diskA")
        assert calls["restart"] == 0

    def test_debounced_on_same_disk_rev(self):
        ctrl, _ = _controller(skew=("boot0", "diskA"))
        assert ctrl.evaluate("notify").first_time is True
        # Same drift on the next tick: no re-notify.
        d2 = ctrl.evaluate("notify")
        assert d2.outcome == sr.IDLE
        assert d2.first_time is False

    def test_new_merge_renotifies(self):
        drift = {"val": ("boot0", "diskA")}
        ctrl, _ = _controller(skew=lambda: drift["val"])
        assert ctrl.evaluate("notify").first_time is True
        assert ctrl.evaluate("notify").first_time is False
        drift["val"] = ("boot0", "diskB")  # a second merge landed
        d = ctrl.evaluate("notify")
        assert d.outcome == sr.NOTIFIED
        assert d.first_time is True
        assert d.disk_rev == "diskB"

    def test_notify_never_restarts_even_when_quiet(self):
        ctrl, calls = _controller(skew=("boot0", "diskA"), active=0)
        ctrl.evaluate("notify")
        assert calls["restart"] == 0


class TestSafeRestartMode:
    def test_restarts_when_quiet(self):
        ctrl, calls = _controller(skew=("boot0", "diskA"), active=0)
        d = ctrl.evaluate("safe-restart")
        assert d.outcome == sr.RESTART_REQUESTED
        assert d.first_time
        assert calls["restart"] == 1

    def test_defers_while_work_in_flight(self):
        ctrl, calls = _controller(skew=("boot0", "diskA"), active=2)
        d = ctrl.evaluate("safe-restart")
        assert d.outcome == sr.RESTART_DEFERRED
        assert calls["restart"] == 0

    def test_deferred_then_restarts_on_quiet_tick(self):
        # Busy on tick 1 (deferred), quiet on tick 2 (restart).
        state = {"active": 3}
        calls = {"restart": 0}
        ctrl = RedeployController(
            detect_skew=lambda: ("boot0", "diskA"),
            count_active_work=lambda: state["active"],
            request_restart=lambda: calls.__setitem__(
                "restart", calls["restart"] + 1
            ),
            logger=logging.getLogger("test.self_redeploy"),
        )
        assert ctrl.evaluate("safe-restart").outcome == sr.RESTART_DEFERRED
        assert calls["restart"] == 0
        state["active"] = 0
        d = ctrl.evaluate("safe-restart")
        assert d.outcome == sr.RESTART_REQUESTED
        assert calls["restart"] == 1
        # The deferred tick already raised the finding, so the restart tick is
        # not "first_time" — no duplicate operator push.
        assert d.first_time is False

    def test_deferred_notify_fires_once(self):
        ctrl, _ = _controller(skew=("boot0", "diskA"), active=5)
        assert ctrl.evaluate("safe-restart").first_time is True
        assert ctrl.evaluate("safe-restart").first_time is False
