"""Self-redeploy policy: react when engine code on disk drifts past boot.

Twice-observed failure (decisions.md 2026-07-17): freshly-merged
dispatcher/executor code sat dead in a long-lived gateway's memory until an
operator manually ran ``hermes gateway restart``. This module turns the
existing boot-vs-disk code-skew signal (:mod:`gateway.code_skew`) into an
action on every dispatcher tick, governed by ``kanban.auto_redeploy``:

  * ``off``          — do nothing.
  * ``notify``       — raise a loud "engine changed, restart pending" finding
                       (a WARNING log + a best-effort operator home-channel
                       push). Default.
  * ``safe-restart`` — additionally self-restart via the gateway restart path,
                       but ONLY at a quiet moment (no in-gateway agent/cron/API
                       work in flight); while anything runs, defer to a later
                       tick (drain-wait). Dispatcher-spawned worker subprocesses
                       are detached (``start_new_session=True``) and survive a
                       gateway restart, so they are never interrupted.

Debounce: the notify fires once per distinct on-disk revision (keyed on the
short disk fingerprint), so an idle tick loop doesn't re-spam the same merge.
The restart itself is naturally one-shot — the process exits — and is
re-attempted each tick only until the gateway is quiet.

The controller is deliberately free of gateway internals: it takes injected
callables (skew detector, in-flight-work counter, restart trigger) so its full
policy matrix is unit-testable without spinning up a gateway.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

OFF = "off"
NOTIFY = "notify"
SAFE_RESTART = "safe-restart"
DEFAULT_MODE = NOTIFY
_VALID_MODES = frozenset({OFF, NOTIFY, SAFE_RESTART})

# evaluate() outcomes
IDLE = "idle"
NOTIFIED = "notified"
RESTART_REQUESTED = "restart_requested"
RESTART_DEFERRED = "restart_deferred"


def normalize_redeploy_mode(raw: object) -> str:
    """Coerce a configured value to a valid mode, defaulting to ``notify``.

    An unknown/typo'd value never escalates to ``safe-restart`` — it falls back
    to the safe default so a misconfiguration can't trigger an auto-restart.
    """
    value = str(raw or "").strip().lower()
    return value if value in _VALID_MODES else DEFAULT_MODE


def resolve_redeploy_mode(load_config: Callable[[], dict]) -> str:
    """Re-read ``kanban.auto_redeploy`` live (like auto-decompose), normalized.

    A missing key or a transient config-read failure both resolve to the
    default (notify) rather than silently disabling the guard.
    """
    try:
        cfg = load_config()
    except Exception:
        return DEFAULT_MODE
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    return normalize_redeploy_mode(kcfg.get("auto_redeploy", DEFAULT_MODE))


@dataclass(frozen=True)
class RedeployDecision:
    """Result of one :meth:`RedeployController.evaluate` tick.

    ``first_time`` is True only on the tick a given ``disk_rev`` is first acted
    on — the caller uses it to fire the (best-effort, async) operator push once
    per merge rather than every tick.
    """

    outcome: str
    boot_rev: Optional[str] = None
    disk_rev: Optional[str] = None
    first_time: bool = False


class RedeployController:
    """Per-tick self-redeploy decision engine (see module docstring)."""

    def __init__(
        self,
        *,
        detect_skew: Callable[[], Optional[Tuple[str, str]]],
        count_active_work: Callable[[], int],
        request_restart: Callable[[], None],
        logger: logging.Logger,
    ) -> None:
        self._detect_skew = detect_skew
        self._count_active_work = count_active_work
        self._request_restart = request_restart
        self._log = logger
        # Short disk fingerprint we've already raised the finding for; keeps
        # an idle tick loop from re-notifying the same merge every interval.
        self._notified_disk_rev: Optional[str] = None

    def evaluate(self, mode: str) -> RedeployDecision:
        """Decide (and, for safe-restart when quiet, trigger) the reaction."""
        mode = normalize_redeploy_mode(mode)
        skew = self._detect_skew()
        if skew is None:
            # Disk matches boot (or is unreadable): clear the debounce so a
            # later genuine re-drift raises a fresh finding.
            self._notified_disk_rev = None
            return RedeployDecision(IDLE)

        boot_rev, disk_rev = skew
        if mode == OFF:
            return RedeployDecision(IDLE, boot_rev, disk_rev)

        first_time = disk_rev != self._notified_disk_rev
        if first_time:
            self._log.warning(
                "self-redeploy: engine changed, restart pending — gateway booted "
                "from %s but engine code on disk is now %s (auto_redeploy=%s). "
                "Merged code will not run until the gateway restarts.",
                boot_rev,
                disk_rev,
                mode,
            )
            self._notified_disk_rev = disk_rev

        if mode == NOTIFY:
            return RedeployDecision(
                NOTIFIED if first_time else IDLE, boot_rev, disk_rev, first_time
            )

        # safe-restart: restart only at a quiet moment; never interrupt work.
        active = self._count_active_work()
        if active > 0:
            if first_time:
                self._log.info(
                    "self-redeploy: restart deferred — %d unit(s) of work in "
                    "flight; will retry on a later quiet tick.",
                    active,
                )
            return RedeployDecision(RESTART_DEFERRED, boot_rev, disk_rev, first_time)

        self._log.warning(
            "self-redeploy: gateway quiet — restarting to load engine code %s "
            "(was %s).",
            disk_rev,
            boot_rev,
        )
        self._request_restart()
        return RedeployDecision(RESTART_REQUESTED, boot_rev, disk_rev, first_time)
