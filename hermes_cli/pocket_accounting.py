"""Own, vendor-agnostic per-pocket usage accounting — workers *and* interactive.

Task ``t_5580f23b`` (operator correction 18.07 21:10). The runtime *ground truth*
for how much a subscription pocket has burned is **not** the client statusline nor
the provider's usage API — it is our own count of every session that ran under the
pocket's login dir, folded into the same 5h/weekly windows the rest of pacing uses.
Statusline/usage-API numbers are used only to *calibrate* that count (a divergence
over a couple percent raises a Проблемы finding — :func:`reconcile`), never as a
runtime dependency.

Two session populations, one pocket:

* **workers** — ACP sessions the executor leased a pocket for; already tagged onto
  the ``token_usage`` ledger with a non-empty ``subscription`` (see
  :mod:`hermes_cli.zeus_tokens`). Counted from the ledger as before.
* **interactive** — an operator/supervisor Claude/Codex session that held *no*
  lease, so nothing stamped its ledger rows. Its usage was silently lost (or, when
  the ACP hook fired without a lease name, booked to ``subscription=''`` and
  counted toward *no* pocket) — the 18.07 incident where a supervision session's
  spend never reached ``work2``. We reclaim it by reading the session logs under
  each pocket's ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` and attributing every session
  *not already counted in the ledger* to the pocket that owns that dir
  (:func:`agent.claude_subscriptions.subscription_for_config_dir`).

**Dedup.** A worker session's log lives under the same login dir as the pocket, so
a naive scan would double-count it. We exclude exactly the session ids that already
carry a real ledger attribution (``subscription != ''``); an un-/mis-attributed
session (``''``) is *not* counted in any pocket's worker total, so counting its log
here reclaims it without double-counting. Verified: a worker's ledger ``session_id``
is the log's file stem / ``sessionId``.

**Token formula.** Matches the ledger's ``total_tokens`` exactly (verified against a
real row): ``input + output + cache_read + cache_creation`` for Claude; Codex's own
``total_tokens`` from each ``token_count`` event.

Everything degrades to zero/empty when a dir or table is absent — never raises.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from hermes_cli import subscription_limits

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"

FINDINGS_SOURCE = "pacing-calibration"

# Default divergence gate: the operator's "расхождение > пары %" — a couple of
# percent between our count and the client's real number is an alarm.
DEFAULT_CALIBRATION_THRESHOLD_PCT = 3.0

# Only files touched since a window began can hold in-window turns; skip the rest
# so a poll never re-reads a pocket's whole history. A small slack absorbs clock
# skew / mtime granularity so a straddling file is never wrongly skipped.
_MTIME_SLACK_SECONDS = 3600.0

# Parse-cache: path -> (mtime, [turns]). Each log is parsed once per mtime, so a
# repeated dashboard poll re-reads only files that actually changed.
_FILE_CACHE: dict[str, tuple[float, list["SessionTurn"]]] = {}
_FILE_CACHE_MAX = 2048

_CODEX_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


@dataclass(frozen=True)
class SessionTurn:
    """One assistant turn's usage, attributed to its session and moment."""

    session_id: str
    ts: float
    tokens: int


# --- Timestamp / token helpers ----------------------------------------------


def _parse_ts(value) -> Optional[float]:
    """ISO-8601 (``...Z``) or epoch number -> epoch seconds, or ``None``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def _claude_turn_tokens(usage: dict) -> int:
    """Ledger-consistent total for a Claude turn: in+out+cache_read+cache_create."""
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


# --- Per-provider log readers -----------------------------------------------


def _read_claude_log(path: Path) -> list[SessionTurn]:
    """Assistant-turn usage from one Claude Code session ``.jsonl``."""
    turns: list[SessionTurn] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                usage = (rec.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = _parse_ts(rec.get("timestamp"))
                if ts is None:
                    continue
                sid = rec.get("sessionId") or rec.get("session_id") or path.stem
                turns.append(SessionTurn(str(sid), ts, _claude_turn_tokens(usage)))
    except OSError:
        return []
    return turns


def _read_codex_log(path: Path) -> list[SessionTurn]:
    """Per-``token_count``-event usage from one Codex rollout ``.jsonl``.

    Codex reports a running ``total_token_usage`` and a per-turn ``last_token_usage``
    on each ``event_msg``/``token_count`` record; we sum the per-turn deltas so the
    total matches the provider's own ``total_tokens`` without double-counting the
    cumulative field. Session id comes from the ``session_meta`` record, else the
    UUID embedded in the ``rollout-...-<uuid>.jsonl`` filename.
    """
    m = _CODEX_UUID_RE.search(path.stem)
    sid = m.group(1) if m else path.stem
    turns: list[SessionTurn] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rtype = rec.get("type")
                payload = rec.get("payload") or {}
                if rtype == "session_meta":
                    meta_id = payload.get("id") or (payload.get("session") or {}).get("id")
                    if meta_id:
                        sid = str(meta_id)
                    continue
                if rtype != "event_msg" or payload.get("type") != "token_count":
                    continue
                last = (payload.get("info") or {}).get("last_token_usage") or {}
                tokens = int(last.get("total_tokens") or 0)
                ts = _parse_ts(rec.get("timestamp"))
                if ts is None or tokens <= 0:
                    continue
                turns.append(SessionTurn(sid, ts, tokens))
    except OSError:
        return []
    return turns


def _log_files(config_dir: str, provider: str, since_ts: Optional[float]) -> list[Path]:
    """Session-log files under a pocket's dir, newest-touched first, mtime-gated."""
    base = Path(config_dir).expanduser()
    if provider == PROVIDER_CODEX:
        roots, pattern = [base / "sessions"], "rollout-*.jsonl"
    else:
        roots, pattern = [base / "projects"], "*.jsonl"
    floor = None if since_ts is None else since_ts - _MTIME_SLACK_SECONDS
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob(pattern):
            try:
                if floor is not None and path.stat().st_mtime < floor:
                    continue
            except OSError:
                continue
            out.append(path)
    return out


def _turns_for_file(path: Path, provider: str) -> list[SessionTurn]:
    """Parsed turns for one log file, memoised by ``(path, mtime)``."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    turns = _read_codex_log(path) if provider == PROVIDER_CODEX else _read_claude_log(path)
    if len(_FILE_CACHE) >= _FILE_CACHE_MAX:
        _FILE_CACHE.clear()
    _FILE_CACHE[key] = (mtime, turns)
    return turns


def iter_session_turns(
    config_dir: str, provider: str, *, since_ts: Optional[float] = None
) -> Iterator[SessionTurn]:
    """Every assistant turn logged under ``config_dir`` (mtime-gated by ``since_ts``)."""
    for path in _log_files(config_dir, provider, since_ts):
        yield from _turns_for_file(path, provider)


# --- Windowed accounting -----------------------------------------------------


def attributed_session_ids(
    conn: sqlite3.Connection, *, since_ts: Optional[float] = None
) -> set[str]:
    """Ledger session ids that already carry a real (non-empty) pocket attribution.

    These are the worker sessions counted in some pocket's ledger total; excluding
    them from the log scan is the dedup that stops a worker being counted twice.
    A ``subscription=''`` row is deliberately *not* here — it is counted toward no
    pocket, so the log scan is free to reclaim it. Missing table → empty set.
    """
    clause = "subscription IS NOT NULL AND subscription != ''"
    params: list = []
    if since_ts is not None:
        clause += " AND ts >= ?"
        params.append(since_ts)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM token_usage WHERE {clause} AND session_id != ''",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r["session_id"] for r in rows}


def interactive_window_tokens(
    config_dir: str,
    provider: str,
    *,
    window_start: float,
    until_ts: Optional[float] = None,
    exclude_sessions: frozenset[str] = frozenset(),
) -> dict:
    """``{total_tokens, turns, sessions}`` burned by interactive sessions in-window.

    Sums per-turn tokens from the pocket's session logs over ``[window_start,
    until_ts]``, skipping any session in ``exclude_sessions`` (the ledger workers).
    ``sessions`` is the count of distinct interactive sessions that contributed.
    """
    total = 0
    turns = 0
    sessions: set[str] = set()
    for turn in iter_session_turns(config_dir, provider, since_ts=window_start):
        if turn.ts < window_start:
            continue
        if until_ts is not None and turn.ts > until_ts:
            continue
        if turn.session_id in exclude_sessions:
            continue
        total += turn.tokens
        turns += 1
        sessions.add(turn.session_id)
    return {"total_tokens": total, "turns": turns, "sessions": len(sessions)}


def _ledger_window_tokens(
    conn: sqlite3.Connection,
    subscription: str,
    window_start: float,
    until_ts: Optional[float],
) -> int:
    """Worker tokens the ledger booked to ``subscription`` in-window (0 on absence)."""
    clause = "subscription = ? AND ts >= ?"
    params: list = [subscription, window_start]
    if until_ts is not None:
        clause += " AND ts <= ?"
        params.append(until_ts)
    try:
        row = conn.execute(
            f"SELECT COALESCE(SUM(total_tokens), 0) AS t FROM token_usage WHERE {clause}",
            tuple(params),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["t"] or 0)


def pocket_usage(
    conn: sqlite3.Connection,
    name: str,
    config_dir: str,
    provider: str,
    *,
    window_start: float,
    until_ts: Optional[float] = None,
) -> dict:
    """Full in-window accounting for one pocket: workers (ledger) + interactive (logs).

    Returns ``{subscription, worker_tokens, interactive_tokens, total_tokens,
    interactive_sessions}``. ``total_tokens`` is the pocket's real burn — the number
    pacing/`/subs`/the Подписки panel should show. ``config_dir=''`` yields
    interactive 0 (nothing to scan) so a dir-less pocket degrades to the ledger count.
    """
    worker = _ledger_window_tokens(conn, name, window_start, until_ts)
    if config_dir:
        exclude = frozenset(attributed_session_ids(conn, since_ts=window_start))
        inter = interactive_window_tokens(
            config_dir, provider, window_start=window_start,
            until_ts=until_ts, exclude_sessions=exclude,
        )
    else:
        inter = {"total_tokens": 0, "turns": 0, "sessions": 0}
    return {
        "subscription": name,
        "worker_tokens": worker,
        "interactive_tokens": inter["total_tokens"],
        "total_tokens": worker + inter["total_tokens"],
        "interactive_sessions": inter["sessions"],
    }


# --- Calibration against the client's real numbers --------------------------


@dataclass(frozen=True)
class Divergence:
    """Our computed usage% drifted from the client's real usage% past the gate."""

    window_label: str
    computed_percent: float
    real_percent: float
    diff_percent: float          # signed: computed - real
    threshold_percent: float


def reconcile(
    computed_percent: Optional[float],
    real_percent: Optional[float],
    *,
    window_label: str,
    threshold_percent: float = DEFAULT_CALIBRATION_THRESHOLD_PCT,
) -> Optional[Divergence]:
    """Flag when our count and the client's real % disagree by more than the gate.

    Pure. ``None`` when either input is missing (nothing to compare — fail open) or
    the gap is within tolerance. The client number is the *reference*; our count is
    what's being checked, so ``diff = computed - real`` (negative = we undercount,
    the 18.07 direction).
    """
    if computed_percent is None or real_percent is None:
        return None
    diff = computed_percent - real_percent
    if abs(diff) <= threshold_percent:
        return None
    return Divergence(
        window_label=window_label,
        computed_percent=round(computed_percent, 1),
        real_percent=round(real_percent, 1),
        diff_percent=round(diff, 1),
        threshold_percent=threshold_percent,
    )


def finding_key(subscription: str, window_label: str) -> str:
    return f"pacing-calibration:{subscription}:{window_label}"


def finding_for_divergence(subscription: str, div: Divergence) -> dict:
    """Render the emit-ready Проблемы finding for a calibration divergence."""
    direction = "занижает" if div.diff_percent < 0 else "завышает"
    title = (
        f"Пейсинг {direction} расход '{subscription}' ({div.window_label}) на "
        f"{abs(div.diff_percent):.0f}%"
    )
    detail = (
        f"Наш собственный подсчёт окна '{div.window_label}' для подписки "
        f"'{subscription}' = {div.computed_percent:.0f}%, реальные данные клиента = "
        f"{div.real_percent:.0f}% (расхождение {div.diff_percent:+.0f}%, порог "
        f"±{div.threshold_percent:.0f}%). Наш учёт — источник правды в рантайме; "
        f"клиентские цифры только калибруют его. Устойчивое расхождение = дыра в "
        f"атрибуции (сессия учтена не в тот карман) или сдвиг окна вендором — "
        f"разберись, не считай лимит сам."
    )
    return {
        "finding_key": finding_key(subscription, div.window_label),
        "title": title,
        "detail": detail,
        "category": "pacing-calibration",
        "severity": "warning",
        "evidence": {
            "subscription": subscription,
            "window": div.window_label,
            "computed_percent": div.computed_percent,
            "real_percent": div.real_percent,
            "diff_percent": div.diff_percent,
            "threshold_percent": div.threshold_percent,
        },
    }


def emit_calibration_finding(
    conn: sqlite3.Connection,
    finding: dict,
    *,
    board: str = "",
    now: Optional[float] = None,
) -> None:
    """Upsert one open calibration finding, keyed ``(board, source, finding_key)``.

    Shares the browse-only ``findings`` contract with
    :mod:`hermes_cli.subscription_limits` (never un-dismisses a human-resolved row).
    """
    now = time.time() if now is None else now
    conn.execute(subscription_limits._FINDINGS_SCHEMA)
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


def calibrate(
    conn: sqlite3.Connection,
    subscription: str,
    *,
    window_label: str,
    computed_percent: Optional[float],
    real_percent: Optional[float],
    board: str = "",
    threshold_percent: float = DEFAULT_CALIBRATION_THRESHOLD_PCT,
    now: Optional[float] = None,
    emit: bool = True,
) -> Optional[dict]:
    """Reconcile one pocket window and push a Проблемы finding on divergence.

    Returns the finding dict when one was raised, else ``None``. Does **not** commit
    — the caller owns the transaction. This is the only calibration seam: the client
    number never feeds the runtime count, it only triggers this alarm.
    """
    div = reconcile(
        computed_percent, real_percent,
        window_label=window_label, threshold_percent=threshold_percent,
    )
    if div is None:
        return None
    finding = finding_for_divergence(subscription, div)
    if emit:
        emit_calibration_finding(conn, finding, board=board, now=now)
    return finding
