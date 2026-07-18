"""Zeus mechanical watchdog — a sealed, LLM-free liveness monitor.

The watchdog lives *outside* the thing it watches: it runs as a standalone
launchd agent (see ``watchdog.plist.template``), not as a gateway cron job, so
it survives a wedged or dead gateway. It is pure mechanics — zero LLM, zero
network except a single ``curl`` to the Telegram Bot API — and depends on the
Python standard library only, so it keeps working even when every agent and the
whole ``hermes`` package are broken.

Layers, each independently unit-testable:

* :mod:`zeus_watchdog.config` — defaults + JSON overlay + token/chat-id resolution;
* :mod:`zeus_watchdog.checks` — IO probes and pure condition evaluators;
* :mod:`zeus_watchdog.state` — pure debounce/sustain/recovery reconciliation;
* :mod:`zeus_watchdog.alert` — Telegram send via direct ``curl`` (injectable);
* :mod:`zeus_watchdog.runner` — one-pass orchestration (``run_once``).
"""

from __future__ import annotations

__all__ = ["config", "checks", "state", "alert", "runner"]
