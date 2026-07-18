"""Debounce, sustain, and recovery bookkeeping — the pure decision core.

The runner persists a tiny JSON state between passes so it can (a) require a
condition to hold for ``sustain_sec`` before alerting, (b) not re-alert the same
problem more than once per ``debounce_sec`` (default 30 min), and (c) emit a
one-shot "ожил" recovery notice when a previously-alerted problem clears.

:func:`reconcile` is pure: given the previous state, the current conditions,
and ``now``, it returns the next state and the list of alerts to send. No IO.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checks import Condition

PROBLEM = "problem"
RECOVERY = "recovery"


@dataclass(frozen=True)
class Alert:
    kind: str  # PROBLEM or RECOVERY
    key: str
    summary: str


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Load the per-key state map; return {} on any failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    problems = data.get("problems") if isinstance(data, dict) else None
    return problems if isinstance(problems, dict) else {}


def save_state(path: Path, problems: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"problems": problems}, indent=2), encoding="utf-8")
    tmp.replace(path)


def reconcile(
    prev: dict[str, dict[str, Any]],
    conditions: list[Condition],
    now: float,
    debounce_sec: int,
) -> tuple[dict[str, dict[str, Any]], list[Alert]]:
    """Fold current conditions into prior state; return (next_state, alerts)."""
    cond_by_key = {c.key: c for c in conditions}
    new: dict[str, dict[str, Any]] = {}
    alerts: list[Alert] = []

    for key, cond in cond_by_key.items():
        st = prev.get(key) or {}
        since = float(st.get("since", now))
        alerted = bool(st.get("alerted", False))
        last_alerted = st.get("last_alerted")
        entry: dict[str, Any] = {
            "since": since,
            "alerted": alerted,
            "last_alerted": last_alerted,
            "summary": cond.summary,
        }
        if now - since >= cond.sustain_sec:
            due = (not alerted) or last_alerted is None or (
                now - float(last_alerted) >= debounce_sec
            )
            if due:
                alerts.append(Alert(PROBLEM, key, cond.summary))
                entry["alerted"] = True
                entry["last_alerted"] = now
        new[key] = entry

    # A key that was actively alerting and is now gone has recovered.
    for key, st in prev.items():
        if key not in cond_by_key and st.get("alerted"):
            alerts.append(Alert(RECOVERY, key, str(st.get("summary", ""))))
            # Resolved: drop it from state entirely.

    return new, alerts
