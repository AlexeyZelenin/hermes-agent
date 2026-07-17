"""Structured engine-room log — the normalization/validation core.

Task t_adf37522. The engine room ("под капотом") gets ONE structured log with
two producers, so an operator can search everything in a single place:

* ``operator`` — what the *native oversight* and crons SAW / DID / DECIDED
  (server-side diagnostic breadcrumbs). Trusted input.
* ``client`` — the FRONTEND's own log: UI actions, WebSocket events, optimistic
  renders, and JS errors, POSTed from the browser. **Untrusted** input, and the
  only place a frontend-only bug leaves a trace: a phantom card that renders but
  never reaches the backend is invisible to every server log (the whole point of
  the card — оператор 01:5x). This module is the boundary that sanitises those
  browser-submitted records before they touch the store.

The DB read/write lives in :mod:`hermes_cli.kanban_db` (``record_log`` /
``record_client_logs`` / ``query_log``); this module is pure and unit-testable:
it defines the vocabulary (sources, severities), the caps, and the
:func:`normalize_client_entry` / :func:`sanitize_client_batch` functions that
clamp hostile or malformed client input to a safe, bounded shape.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

# --- Vocabulary -------------------------------------------------------------

SOURCE_OPERATOR = "operator"
SOURCE_CLIENT = "client"
SOURCES: frozenset[str] = frozenset({SOURCE_OPERATOR, SOURCE_CLIENT})

# Severity ladder, weakest → strongest. ``severity_at_least`` uses this order to
# implement the ``severity`` (minimum) query filter.
SEVERITIES: tuple[str, ...] = ("debug", "info", "warn", "error")
_SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}
DEFAULT_SEVERITY = "info"

# --- Caps (defensive bounds on untrusted client input) ----------------------

# A single POST /client-log may carry at most this many entries; extras are
# dropped (the browser buffers and re-sends, so nothing is silently lost as long
# as the buffer flushes often enough). Keeps one hostile/buggy tab from writing
# an unbounded batch.
MAX_BATCH = 200
# Per-field truncation. ``event``/``category``/``task_id``/``session_id`` are
# short identifiers; ``payload`` is a small JSON blob (a few fields, not a heap
# dump). Anything larger is truncated rather than rejected, so a slightly-too-big
# line still leaves a usable trace.
MAX_EVENT_CHARS = 120
MAX_CATEGORY_CHARS = 40
MAX_ID_CHARS = 80
MAX_PAYLOAD_CHARS = 4000
# Client-supplied ``created_at`` (epoch seconds) is only trusted within this many
# seconds of the server clock; anything outside the window (skewed/hostile clock)
# is replaced with the server ``now``. One day each way absorbs timezone-confused
# or briefly-offline tabs without letting a client forge ancient/future rows.
CLOCK_SKEW_TOLERANCE_SECONDS = 86_400


def normalize_severity(value: Any) -> str:
    """Coerce an arbitrary client value to a known severity (default ``info``)."""
    if isinstance(value, str) and value.strip().lower() in _SEVERITY_ORDER:
        return value.strip().lower()
    return DEFAULT_SEVERITY


def severity_at_least(severity: str, minimum: str) -> bool:
    """True iff ``severity`` is at or above ``minimum`` on the ladder.

    Unknown severities are treated as the weakest rung, so a bogus stored value
    never masquerades as an error in a ``severity=error`` filter.
    """
    return _SEVERITY_ORDER.get(severity, -1) >= _SEVERITY_ORDER.get(minimum, 0)


def _clean_str(value: Any, limit: int) -> Optional[str]:
    """Trim+truncate a value to a bounded string, or ``None`` if empty/not str-able."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def _clean_payload(value: Any) -> Optional[str]:
    """Serialise a client payload to a capped JSON string, or ``None``.

    Only JSON objects are kept (a log line's structured fields); scalars/arrays
    are wrapped under ``{"value": ...}`` so the column is always an object. Any
    value that won't serialise, or that exceeds :data:`MAX_PAYLOAD_CHARS` after
    dumping, is dropped to ``None`` — a missing payload never blocks the line.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        value = {"value": value}
    try:
        dumped = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(dumped) > MAX_PAYLOAD_CHARS:
        return None
    return dumped


def normalize_client_entry(raw: Any, *, now: int) -> Optional[dict[str, Any]]:
    """Sanitise one browser-submitted log record into a safe insert dict.

    Returns ``None`` for anything that isn't a dict with a usable ``event`` name
    (the one required field). Everything else is clamped: ``source`` is forced to
    ``client`` (a browser can never write an ``operator`` line), ``severity`` is
    snapped to the known ladder, string fields are truncated, the payload is
    capped, and ``created_at`` is only honoured inside the clock-skew window.
    """
    if not isinstance(raw, dict):
        return None
    event = _clean_str(raw.get("event"), MAX_EVENT_CHARS)
    if not event:
        return None
    created = raw.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        created = int(created)
        if abs(created - now) > CLOCK_SKEW_TOLERANCE_SECONDS:
            created = now
    else:
        created = now
    return {
        "source": SOURCE_CLIENT,
        "severity": normalize_severity(raw.get("severity")),
        "category": _clean_str(raw.get("category"), MAX_CATEGORY_CHARS),
        "event": event,
        "task_id": _clean_str(raw.get("task_id"), MAX_ID_CHARS),
        "session_id": _clean_str(raw.get("session_id"), MAX_ID_CHARS),
        "payload": _clean_payload(raw.get("payload")),
        "created_at": created,
    }


def sanitize_client_batch(
    raw: Any,
    *,
    now: int,
    max_entries: int = MAX_BATCH,
    session_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Normalise a client batch, capping length and dropping invalid entries.

    ``raw`` is the ``entries`` array from the POST body. ``session_id`` (from the
    top-level body) back-fills any entry that didn't carry its own, so all lines
    from one page load correlate even if an individual line omitted it. Entries
    past ``max_entries`` are dropped; the count returned to the caller reflects
    what was actually accepted.
    """
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    batch_session = _clean_str(session_id, MAX_ID_CHARS)
    out: list[dict[str, Any]] = []
    for item in raw:
        if len(out) >= max_entries:
            break
        entry = normalize_client_entry(item, now=now)
        if entry is None:
            continue
        if entry["session_id"] is None:
            entry["session_id"] = batch_session
        out.append(entry)
    return out
