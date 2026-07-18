"""Empirically-measured subscription session limits + vendor-shift detection.

Task ``t_e38bbe56`` (operator idea 02:0x). The provider never tells us the real
token budget of a 5h session window — but every usage-limit death *reveals* it:
the tokens we burned through that pocket since the window opened, up to the
limit-hit, is a direct measurement of the cap. This module turns each
``mark_limited`` event into one such measurement and accumulates a per-pocket
series. **Zero LLM** — it is pure mechanics, like the log-watcher's gate.

Three stages, all deterministic:

* **measure** (:func:`record_measurement`) — on a limit-hit, sum the pocket's
  ``token_usage`` over the 5h window ``[reset_at - 5h, now]`` and store it. Only
  windows that *actually reached* a limit-hit are recorded, so every stored
  number is a true cap, not a lower bound (a window that merely idled to reset
  would understate it — we never record those).
* **detect shift** (:func:`detect_shift`) — compare the newest measurements
  against the historical baseline (median of the priors). Gate exactly like the
  log-watcher's once-vs-recurring rule: a *single* off measurement is noise; only
  a **sustained** run (the last ``sustained`` measurements all past ±threshold on
  the same side) is flagged. That is how a genuine vendor change of window sizes
  (Anthropic/OpenAI have done this) surfaces itself, while day-to-day jitter does
  not.
* **push** (:func:`emit_finding`) — a flagged shift is upserted into the shared
  Проблемы (``findings``) store as a browse-only draft (task ``t_e9b93153``); the
  LLM *interprets* the shift there, it never computes it. See
  :data:`REFLECTION_HINT` for the one line the interpreting reflection's system
  prompt should carry.

The measurement also **feeds pacing v2** (task ``t_df88ba4b``): a real measured
session cap gives the circuit-breaker a precise "range remaining" instead of the
weekly-share estimate — see :func:`measured_session_limit`, consumed by
:mod:`hermes_cli.zeus_pacing`.

Everything degrades to nothing when the zeus ledger is absent or a table is
missing (matching :mod:`hermes_cli.logwatcher` / :mod:`hermes_cli.zeus_tokens`):
callers get ``None``/``[]`` and the caller-facing hook :func:`on_limit_hit` is
best-effort — a measurement failure never blocks a subscription's cooldown.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

# The nested 5h session window Claude subscriptions meter inside the weekly one —
# the wall a burst actually trips. Mirrors
# ``agent.claude_subscriptions.LIMIT_WINDOW_SECONDS`` /
# ``hermes_cli.zeus_pacing._FIVE_HOUR_WINDOW_SECONDS``.
SESSION_WINDOW_SECONDS = 5 * 3600

FINDINGS_SOURCE = "subscription-limits"

# The shift gate. A new measurement must differ from the baseline by more than
# this fraction to count as "off"; and the last ``DEFAULT_SUSTAINED`` measurements
# must ALL be off on the same side before a shift is flagged (the log-watcher's
# once-vs-recurring rule — one off window is jitter, a sustained run is a real
# vendor change). ``DEFAULT_BASELINE_MIN`` prior measurements are required before
# any baseline is trusted, so the very first windows never false-positive.
DEFAULT_SHIFT_THRESHOLD_PCT = 15.0
DEFAULT_SUSTAINED = 2
DEFAULT_BASELINE_MIN = 3

# How many recent measurements the series/baseline logic looks back over, and how
# many the pacing feed medians. Bounded so a long-lived pocket's baseline stays
# responsive to the current regime rather than being dragged by ancient windows.
_SERIES_LOOKBACK = 24
_PACING_RECENT = 5
_PACING_MIN_SAMPLES = 2

# The one concise line the *existing* reflection's system prompt should carry so
# it knows what a pushed shift finding means (the operator's call: reuse the
# reflection, do not build a new interpreting agent). The finding detail already
# states this; the prompt line primes the reflection to treat it as a signal.
REFLECTION_HINT = (
    "Если в Проблемах есть находка source=subscription-limits ('измеренный лимит "
    "подписки сдвинулся') — это эмпирический сигнал, что вендор изменил объём окна; "
    "разберись почему и что делать, лимит уже измерен детерминированно, не считай его сам."
)

_MEASUREMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_limit_measurements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription  TEXT NOT NULL,
    ts            REAL NOT NULL,
    window_start  REAL NOT NULL,
    window_reset  REAL NOT NULL,
    measured_tokens INTEGER NOT NULL,
    turns         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sublimit_sub
    ON subscription_limit_measurements(subscription, ts);
"""

# Shared zeus ``findings`` schema — identical DDL across every emitter (see the
# note in hermes_cli.logwatcher); IF NOT EXISTS so whichever process runs first
# creates it and the ``source`` column keeps the emitters from colliding.
_FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    board         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    finding_key   TEXT NOT NULL,
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    category      TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL DEFAULT 'info',
    action_json   TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending',
    status        TEXT NOT NULL DEFAULT 'open',
    snooze_until  REAL,
    converted_task_id TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE(board, source, finding_key)
);
"""


# --- Pure measurement + shift detection -------------------------------------


def _median(values: list[float]) -> Optional[float]:
    """Median of ``values`` (sorted middle / mean of the two middles), or None."""
    n = len(values)
    if n == 0:
        return None
    s = sorted(values)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@dataclass(frozen=True)
class ShiftVerdict:
    """A sustained move of the measured cap away from its historical baseline."""

    direction: str          # "up" or "down"
    baseline: float         # median of the prior measurements
    recent_median: float    # median of the sustained recent run
    shift_percent: float    # signed % move of recent_median vs baseline
    samples: int            # measurements considered
    threshold_percent: float
    sustained: int


def detect_shift(
    series: list[int],
    *,
    threshold_percent: float = DEFAULT_SHIFT_THRESHOLD_PCT,
    sustained: int = DEFAULT_SUSTAINED,
    baseline_min: int = DEFAULT_BASELINE_MIN,
) -> Optional[ShiftVerdict]:
    """Flag a *sustained* shift of the measured cap, else ``None`` (pure).

    ``series`` is the pocket's measured-token series oldest→newest. The last
    ``sustained`` values are the "recent run"; the baseline is the median of the
    values before it (require at least ``baseline_min`` of them, else no verdict —
    too little history to judge). A shift is flagged only when *every* value in
    the recent run lies more than ``threshold_percent`` from the baseline **on the
    same side** — one off window is jitter (mirrors the log-watcher gate). ``None``
    on insufficient data or a non-positive baseline.
    """
    if sustained < 1 or len(series) < baseline_min + sustained:
        return None
    prior = [float(v) for v in series[:-sustained]]
    recent = [float(v) for v in series[-sustained:]]
    baseline = _median(prior)
    if baseline is None or baseline <= 0:
        return None
    hi = baseline * (1.0 + threshold_percent / 100.0)
    lo = baseline * (1.0 - threshold_percent / 100.0)
    if all(v > hi for v in recent):
        direction = "up"
    elif all(v < lo for v in recent):
        direction = "down"
    else:
        return None
    recent_median = _median(recent) or 0.0
    return ShiftVerdict(
        direction=direction,
        baseline=baseline,
        recent_median=recent_median,
        shift_percent=(recent_median - baseline) / baseline * 100.0,
        samples=len(series),
        threshold_percent=threshold_percent,
        sustained=sustained,
    )


def finding_key(subscription: str) -> str:
    return f"sublimit-shift:{subscription}"


def finding_for_shift(subscription: str, verdict: ShiftVerdict) -> dict[str, Any]:
    """Render the emit-ready Проблемы finding for a flagged shift."""
    arrow = "вырос" if verdict.direction == "up" else "упал"
    title = (
        f"Измеренный лимит подписки '{subscription}' {arrow} на "
        f"{abs(verdict.shift_percent):.0f}%"
    )
    detail = (
        f"Эмпирически измеренный лимит 5-часового окна для подписки "
        f"'{subscription}' {arrow}: базлайн ≈ {verdict.baseline:,.0f} токенов, "
        f"последние {verdict.sustained} окна ≈ {verdict.recent_median:,.0f} "
        f"(порог {verdict.threshold_percent:.0f}%). Это устойчивый сдвиг, не "
        f"разовый шум — вероятно вендор изменил объём окна. Сигнал к разбору: "
        f"лимит измерен детерминированно на limit-hit, LLM интерпретирует, не "
        f"считает."
    )
    return {
        "finding_key": finding_key(subscription),
        "title": title,
        "detail": detail,
        "category": "subscription-limit-shift",
        "severity": "warning",
        "evidence": {
            "subscription": subscription,
            "direction": verdict.direction,
            "baseline_tokens": round(verdict.baseline),
            "recent_median_tokens": round(verdict.recent_median),
            "shift_percent": round(verdict.shift_percent, 1),
            "threshold_percent": verdict.threshold_percent,
            "sustained": verdict.sustained,
            "samples": verdict.samples,
        },
    }


# --- Persistence + ledger reads ---------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the measurement table (and the shared findings table)."""
    conn.executescript(_MEASUREMENTS_SCHEMA)
    conn.execute(_FINDINGS_SCHEMA)


def _window_tokens(
    conn: sqlite3.Connection, subscription: str, window_start: float, until_ts: float
) -> Optional[tuple[int, int]]:
    """``(total_tokens, turns)`` this pocket burned in ``[window_start, until_ts]``.

    ``None`` when the ``token_usage`` ledger table is absent (zeus plugin never
    ran), so the caller degrades rather than raising.
    """
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t, COUNT(*) AS n "
            "FROM token_usage WHERE subscription = ? AND ts >= ? AND ts <= ?",
            (subscription, window_start, until_ts),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["t"] or 0), int(row["n"] or 0)


def record_measurement(
    conn: sqlite3.Connection,
    subscription: str,
    *,
    reset_at: float,
    now: float,
) -> Optional[dict[str, Any]]:
    """Measure and store the session cap revealed by a limit-hit.

    Sums the pocket's ``token_usage`` over the 5h window ending at ``reset_at``
    (i.e. ``[reset_at - 5h, now]``) and inserts one measurement row. Returns the
    stored measurement dict, or ``None`` when there is no ledger to read, or the
    measured total is ``<= 0`` — a zero means every token this window was burned
    *outside* Hermes (the operator's own interactive use), so our number would be
    a meaningless understatement and is deliberately not recorded.
    """
    window_start = reset_at - SESSION_WINDOW_SECONDS
    measured = _window_tokens(conn, subscription, window_start, now)
    if measured is None:
        return None
    total, turns = measured
    if total <= 0:
        return None
    conn.execute(
        "INSERT INTO subscription_limit_measurements "
        "(subscription, ts, window_start, window_reset, measured_tokens, turns) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subscription, now, window_start, reset_at, total, turns),
    )
    return {
        "subscription": subscription,
        "ts": now,
        "window_start": window_start,
        "window_reset": reset_at,
        "measured_tokens": total,
        "turns": turns,
    }


def load_series(
    conn: sqlite3.Connection, subscription: str, *, lookback: int = _SERIES_LOOKBACK
) -> list[int]:
    """The pocket's measured-token series, oldest→newest, capped to ``lookback``.

    Returns the most recent ``lookback`` measurements in chronological order
    (what :func:`detect_shift` expects). Missing table → ``[]``.
    """
    try:
        rows = conn.execute(
            "SELECT measured_tokens FROM subscription_limit_measurements "
            "WHERE subscription = ? ORDER BY ts DESC LIMIT ?",
            (subscription, max(1, lookback)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r["measured_tokens"]) for r in reversed(rows)]


def measured_session_limit(
    conn: Optional[sqlite3.Connection],
    subscription: str,
    *,
    recent: int = _PACING_RECENT,
    min_samples: int = _PACING_MIN_SAMPLES,
) -> Optional[float]:
    """Best empirical 5h-session cap for a pocket — the pacing v2 feed.

    Median of the pocket's most recent ``recent`` measurements, or ``None`` when
    fewer than ``min_samples`` exist (too little to trust) or the ledger is
    absent. The median resists a single fluke window. Used by
    :mod:`hermes_cli.zeus_pacing` to give the circuit-breaker a precise budget
    instead of the weekly-share estimate. Because an unmeasured external burn can
    only make our number *lower* than the true cap, using it errs toward tripping
    early — a safe direction for pacing.
    """
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT measured_tokens FROM subscription_limit_measurements "
            "WHERE subscription = ? ORDER BY ts DESC LIMIT ?",
            (subscription, max(1, recent)),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    values = [float(r["measured_tokens"]) for r in rows]
    if len(values) < max(1, min_samples):
        return None
    return _median(values)


def emit_finding(
    conn: sqlite3.Connection,
    finding: dict[str, Any],
    *,
    board: str = "",
    now: Optional[float] = None,
) -> None:
    """Upsert one open shift finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes title/detail/severity/evidence and ``updated_at`` but
    preserves ``created_at`` and never un-dismisses a finding a human resolved
    (dismissed/snoozed/accepted stay put) — the browse-only contract shared with
    :mod:`hermes_cli.logwatcher`.
    """
    now = time.time() if now is None else now
    conn.execute(_FINDINGS_SCHEMA)
    conn.execute(
        "INSERT INTO findings "
        "(board, source, finding_key, title, detail, evidence_json, category, "
        " severity, created_at, updated_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open') "
        "ON CONFLICT(board, source, finding_key) DO UPDATE SET "
        "  title=excluded.title, detail=excluded.detail, "
        "  evidence_json=excluded.evidence_json, category=excluded.category, "
        "  severity=excluded.severity, updated_at=excluded.updated_at, "
        "  status=CASE WHEN findings.status IN ('dismissed','snoozed','accepted') "
        "              THEN findings.status ELSE 'open' END",
        (board, FINDINGS_SOURCE, finding["finding_key"], finding["title"],
         finding["detail"], json.dumps(finding.get("evidence"), ensure_ascii=False),
         finding.get("category", ""), finding.get("severity", "info"), now, now),
    )


# --- Orchestration: the limit-hit hook --------------------------------------


def on_limit_hit(
    conn: sqlite3.Connection,
    subscription: str,
    *,
    reset_at: float,
    now: float,
    board: str = "",
    threshold_percent: float = DEFAULT_SHIFT_THRESHOLD_PCT,
    sustained: int = DEFAULT_SUSTAINED,
    baseline_min: int = DEFAULT_BASELINE_MIN,
    emit: bool = True,
) -> Optional[dict[str, Any]]:
    """Record a measurement for this limit-hit and push a shift finding if any.

    The one call ``mark_limited`` makes: measure the window's revealed cap
    (:func:`record_measurement`), then run the sustained-shift gate over the
    pocket's series (:func:`detect_shift`) and, on a flag, upsert the Проблемы
    finding. Returns the finding dict when one was pushed, else ``None``. Reuses
    the caller's open zeus.db connection and does **not** commit — the caller owns
    the transaction (``mark_limited`` commits its own ``with conn`` block).
    """
    ensure_schema(conn)
    measurement = record_measurement(conn, subscription, reset_at=reset_at, now=now)
    if measurement is None:
        return None
    series = load_series(conn, subscription)
    verdict = detect_shift(
        series, threshold_percent=threshold_percent,
        sustained=sustained, baseline_min=baseline_min,
    )
    if verdict is None:
        return None
    finding = finding_for_shift(subscription, verdict)
    if emit:
        emit_finding(conn, finding, board=board, now=now)
    return finding
