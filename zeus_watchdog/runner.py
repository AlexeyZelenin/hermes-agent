"""One-pass orchestration: probe → evaluate → reconcile → alert → persist.

``run_once`` is what the launchd agent invokes every interval. It is a single
shot (no internal loop): gather a snapshot, decide, act, save state, exit. If
the process crashes, launchd simply runs it again next interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import alert as alert_mod
from . import checks
from .config import Config
from .state import Alert, load_state, reconcile, save_state


@dataclass
class PassResult:
    conditions: list[str]
    alerts: list[Alert]
    delivered: int


def run_once(
    cfg: Config,
    now: Optional[float] = None,
    sender: alert_mod.Sender = alert_mod.curl_sender,
    dry_run: bool = False,
) -> PassResult:
    """Run a single watchdog pass. Returns a summary of what happened."""
    now = time.time() if now is None else now
    probe = checks.gather(cfg, now=now)
    conditions = checks.evaluate(probe, cfg)
    prev = load_state(cfg.state_file)
    next_state, alerts = reconcile(prev, conditions, now, cfg.debounce_sec)

    delivered = 0
    if dry_run:
        # No sends and no state mutation — pure observation.
        return PassResult([c.key for c in conditions], alerts, 0)

    delivered = alert_mod.dispatch(cfg, alerts, sender=sender)
    save_state(cfg.state_file, next_state)
    return PassResult([c.key for c in conditions], alerts, delivered)
