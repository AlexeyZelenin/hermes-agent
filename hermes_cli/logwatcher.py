"""Log-watcher — a reflection-extension regular that surfaces *recurring* log
anomalies (not one-time noise) into the shared Проблемы store.

Operator model (task t_ed00fffe): the night-shift operator glances at the logs
for anomalies, but only acts on the ones that *keep happening* — a single scary
line is usually a blip, the same line three times is a real defect worth a fix.
This automates exactly that gate:

* **scan** — read the new tail of each watched log and pick out anomaly lines
  (level ``WARNING`` and above, configurable);
* **sign** — collapse each line to a stable *signature* (dedup key) by
  normalising out the volatile parts (timestamps, pids, hex ids, quoted values,
  numbers) so "same class of error" maps to one key regardless of the instance;
* **gate (once vs recurring)** — the FIRST time a signature is seen we merely
  RECORD it in the ``logwatch_occurrences`` table and do *nothing* (assume
  one-time noise); on the Nth sighting (``threshold``, default 2) the signature
  is PROMOTED into the shared ``findings`` store as a draft card on the Проблемы
  board, so a human/agent can investigate *why*.

Push policy is **browse-only**: the watcher only ever pushes findings (draft
cards) — it never creates real backlog cards or takes any action itself; the
Проблемы UI's *accept* verb (see :mod:`hermes_cli.problems`) is what materialises
a finding into a real card. The core is **LLM-free** pure mechanics; any later
"investigate why" LLM step runs under the accepted card and is attributed to
``source=logwatcher`` via the same cron run-session prefix the zeus ledger uses
for :mod:`hermes_cli.integrity_agent` (documented there — no separate accounting
lives here).

Everything degrades to nothing when the zeus ledger is absent: the occurrence
gate needs a place to persist counts, so without ``zeus.db`` there is no push
path (matching :mod:`hermes_cli.regular_crons` / :mod:`hermes_cli.integrity_agent`).

Pure and dependency-injectable: :func:`parse_line`, :func:`signature`,
:func:`extract_anomalies` and :func:`gate` take plain strings/dicts and unit-test
without any file, board, or ledger; :func:`run_logwatch_scan` wires the real log
tail, the occurrence store and the findings push.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

FINDINGS_SOURCE = "logwatcher"

# The gate: this many sightings of one signature promotes it from "recorded
# noise" to a draft finding. Deliberately low but > 1 so a single blip never
# reaches Проблемы. Configurable per scan.
DEFAULT_PROMOTE_THRESHOLD = 2

# Anomaly floor. A line at this level or above is a candidate signature; below
# it is ignored. Ordered rank so "warning and up" is a single comparison.
_LEVEL_RANK: dict[str, int] = {
    "DEBUG": 0, "INFO": 1, "WARNING": 2, "WARN": 2,
    "ERROR": 3, "CRITICAL": 4, "FATAL": 4,
}
DEFAULT_MIN_LEVEL = "WARNING"

# Level -> finding severity (the store's taxonomy; see hermes_cli.problems).
_SEVERITY_BY_LEVEL: dict[str, str] = {
    "WARNING": "warning", "WARN": "warning",
    "ERROR": "error", "CRITICAL": "critical", "FATAL": "critical",
}

# Logs watched by default, under ``$HERMES_HOME/logs`` (falls back to ~/.hermes).
_DEFAULT_LOG_NAMES = ("errors.log", "gateway.log", "agent.log")


# --- Log-line parsing + signature (pure) ------------------------------------

# ``2026-07-18 05:47:57,486 WARNING gateway.run: <message>`` — the stdlib
# logging default. Continuation lines (tracebacks) don't match and are skipped.
_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,\d]*\s+"
    r"(?P<level>[A-Z]+)\s+(?P<logger>[\w.]+):\s*(?P<message>.*)$"
)

# Volatile-token scrubbers, applied in order. Quotes first (they wrap session
# keys / ids / empty values), then hex-ish tokens (require a letter so pure
# numbers fall through to the number rule), then bare numbers.
_QUOTED_RE = re.compile(r"""(['"]).*?\1""")
_HEXISH_RE = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{6,}\b")
_NUMBER_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Sighting:
    """One anomaly line reduced to its signature plus a human sample."""

    signature: str
    level: str
    logger: str
    sample: str
    source_log: str


def parse_line(line: str) -> Optional[tuple[str, str, str]]:
    """Return ``(level, logger, message)`` for a standard log line, else None."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    return m.group("level"), m.group("logger"), m.group("message")


def normalize_message(message: str) -> str:
    """Collapse a message to its volatile-free skeleton (the dedup core)."""
    s = _QUOTED_RE.sub("<v>", message)
    s = _HEXISH_RE.sub("<hex>", s)
    s = _NUMBER_RE.sub("<n>", s)
    return _WS_RE.sub(" ", s).strip()


def signature(level: str, logger: str, message: str) -> str:
    """Stable dedup key: level + logger + normalised message."""
    return f"{level}|{logger}|{normalize_message(message)}"


def is_anomaly(level: str, min_level: str = DEFAULT_MIN_LEVEL) -> bool:
    """True if ``level`` is at or above the anomaly floor."""
    floor = _LEVEL_RANK.get(min_level.upper(), _LEVEL_RANK[DEFAULT_MIN_LEVEL])
    return _LEVEL_RANK.get(level.upper(), 1) >= floor


def extract_anomalies(
    lines: Iterable[str], source_log: str, min_level: str = DEFAULT_MIN_LEVEL
) -> list[Sighting]:
    """Parse ``lines`` and return a :class:`Sighting` per anomaly-level line."""
    out: list[Sighting] = []
    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            continue
        level, logger, message = parsed
        if not is_anomaly(level, min_level):
            continue
        out.append(Sighting(
            signature=signature(level, logger, message),
            level=level.upper(), logger=logger,
            sample=line.strip()[:500], source_log=source_log,
        ))
    return out


def tally_sightings(sightings: Iterable[Sighting]) -> dict[str, dict[str, Any]]:
    """Aggregate this pass's sightings per signature (count + first sample)."""
    tally: dict[str, dict[str, Any]] = {}
    for s in sightings:
        row = tally.get(s.signature)
        if row is None:
            tally[s.signature] = {
                "count": 1, "sample": s.sample, "level": s.level,
                "logger": s.logger, "source_log": s.source_log,
            }
        else:
            row["count"] += 1
    return tally


# --- Occurrence record + the once-vs-recurring gate (pure) ------------------


@dataclass
class Occurrence:
    """A signature's running state in the ``logwatch_occurrences`` table."""

    signature: str
    count: int
    first_seen_at: float
    last_seen_at: float
    sample: str
    level: str
    source_log: str
    promoted_at: Optional[float] = None


def gate(
    prior: dict[str, Occurrence],
    tally: dict[str, dict[str, Any]],
    *,
    threshold: int = DEFAULT_PROMOTE_THRESHOLD,
    now: float,
) -> tuple[list[Occurrence], list[dict[str, Any]]]:
    """Apply the once-vs-recurring gate to this pass's sightings.

    Pure: given the stored occurrences and the signatures seen this pass, return
    ``(updated_records, findings)``. A signature whose *total* count reaches
    ``threshold`` yields a finding (recurring → draft card); below threshold it
    is merely recorded (one-time noise, no finding). Idempotent — a signature
    already above threshold keeps re-emitting a refreshed finding each pass, but
    its ``promoted_at`` (first crossing) is preserved.
    """
    records: list[Occurrence] = []
    findings: list[dict[str, Any]] = []
    for sig, seen in tally.items():
        p = prior.get(sig)
        count = (p.count if p else 0) + int(seen["count"])
        first = p.first_seen_at if p else now
        crossed = count >= threshold
        promoted_at = (p.promoted_at if p and p.promoted_at is not None
                       else (now if crossed else None))
        rec = Occurrence(
            signature=sig, count=count, first_seen_at=first, last_seen_at=now,
            sample=seen["sample"], level=seen["level"],
            source_log=seen["source_log"], promoted_at=promoted_at,
        )
        records.append(rec)
        if crossed:
            findings.append(_finding_for(rec, threshold))
    return records, findings


def finding_key(sig: str) -> str:
    return f"logwatch:{sig}"


def _finding_for(rec: Occurrence, threshold: int) -> dict[str, Any]:
    """Render the emit-ready finding dict for a promoted signature."""
    _, logger, skeleton = rec.signature.split("|", 2)
    severity = _SEVERITY_BY_LEVEL.get(rec.level, "warning")
    title = f"Повторяющаяся аномалия в логах ({rec.level}): {skeleton[:120]}"
    detail = (
        f"Сигнатура встретилась {rec.count} раз(а) (порог {threshold}) — уже не "
        f"разовый шум. Источник: {rec.source_log}. Логгер: {logger}. "
        f"Пример строки: {rec.sample}"
    )
    return {
        "signature": rec.signature,
        "finding_key": finding_key(rec.signature),
        "title": title,
        "detail": detail,
        "category": "log-anomaly",
        "severity": severity,
        "evidence": {
            "signature": rec.signature, "count": rec.count, "level": rec.level,
            "logger": logger, "source_log": rec.source_log,
            "sample": rec.sample, "first_seen_at": rec.first_seen_at,
            "last_seen_at": rec.last_seen_at, "threshold": threshold,
        },
    }


# --- Log tail cursor (IO, best-effort) --------------------------------------


@dataclass(frozen=True)
class Cursor:
    """Where we last stopped reading a log: byte offset + inode/size for rotation."""

    offset: int = 0
    inode: int = 0
    size: int = 0


def eof_cursor(path: os.PathLike | str) -> Optional[Cursor]:
    """A cursor pointing at the current end of ``path`` (first-attach = tail)."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return Cursor(offset=st.st_size, inode=st.st_ino, size=st.st_size)


def read_new_lines(
    path: os.PathLike | str, cursor: Cursor
) -> tuple[list[str], Cursor]:
    """Read complete lines appended since ``cursor``; return ``(lines, cursor')``.

    Handles rotation/truncation (inode change or shrink → re-read from 0) and
    only advances past the last newline, so a half-written trailing line is left
    for the next pass. Any IO error degrades to ``([], cursor)`` (no progress).
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return [], cursor
    offset = cursor.offset
    if st.st_ino != cursor.inode or st.st_size < offset:
        offset = 0  # rotated or truncated -> start over
    if st.st_size <= offset:
        return [], Cursor(offset=st.st_size, inode=st.st_ino, size=st.st_size)
    try:
        with open(p, "rb") as fh:
            fh.seek(offset)
            data = fh.read(st.st_size - offset)
    except OSError:
        return [], cursor
    nl = data.rfind(b"\n")
    if nl < 0:
        return [], Cursor(offset=offset, inode=st.st_ino, size=st.st_size)
    chunk = data[: nl + 1]
    text = chunk.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines, Cursor(offset=offset + len(chunk), inode=st.st_ino, size=st.st_size)


# --- Persistence: occurrence + cursor tables --------------------------------

_OCCURRENCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS logwatch_occurrences (
    signature     TEXT PRIMARY KEY,
    count         INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    sample        TEXT NOT NULL DEFAULT '',
    level         TEXT NOT NULL DEFAULT '',
    source_log    TEXT NOT NULL DEFAULT '',
    promoted_at   REAL
);
"""

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS logwatch_cursor (
    path       TEXT PRIMARY KEY,
    offset     INTEGER NOT NULL DEFAULT 0,
    inode      INTEGER NOT NULL DEFAULT 0,
    size       INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""

# Shared zeus ``findings`` schema — identical DDL across the regular processes
# (see the note in hermes_cli.integrity_agent); IF NOT EXISTS so whichever runs
# first creates it and the ``source`` column keeps the emitters from colliding.
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the watcher's own tables (and the shared findings table)."""
    conn.execute(_OCCURRENCES_SCHEMA)
    conn.execute(_CURSOR_SCHEMA)
    conn.execute(_FINDINGS_SCHEMA)


def load_occurrences(conn: sqlite3.Connection) -> dict[str, Occurrence]:
    """Read the full occurrence table into ``{signature: Occurrence}``."""
    try:
        rows = conn.execute(
            "SELECT signature, count, first_seen_at, last_seen_at, sample, "
            "level, source_log, promoted_at FROM logwatch_occurrences"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, Occurrence] = {}
    for r in rows:
        out[r["signature"]] = Occurrence(
            signature=r["signature"], count=int(r["count"]),
            first_seen_at=float(r["first_seen_at"]),
            last_seen_at=float(r["last_seen_at"]), sample=r["sample"] or "",
            level=r["level"] or "", source_log=r["source_log"] or "",
            promoted_at=(None if r["promoted_at"] is None else float(r["promoted_at"])),
        )
    return out


def save_occurrences(conn: sqlite3.Connection, records: Iterable[Occurrence]) -> None:
    """Upsert occurrence records (idempotent per signature)."""
    for rec in records:
        conn.execute(
            "INSERT INTO logwatch_occurrences "
            "(signature, count, first_seen_at, last_seen_at, sample, level, "
            " source_log, promoted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(signature) DO UPDATE SET "
            "  count=excluded.count, last_seen_at=excluded.last_seen_at, "
            "  sample=excluded.sample, level=excluded.level, "
            "  source_log=excluded.source_log, "
            "  promoted_at=COALESCE(logwatch_occurrences.promoted_at, excluded.promoted_at)",
            (rec.signature, rec.count, rec.first_seen_at, rec.last_seen_at,
             rec.sample, rec.level, rec.source_log, rec.promoted_at),
        )


def load_cursor(conn: sqlite3.Connection, path: str) -> Optional[Cursor]:
    """Stored read cursor for ``path``, or None if this log is not yet tracked."""
    try:
        row = conn.execute(
            "SELECT offset, inode, size FROM logwatch_cursor WHERE path=?",
            (path,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return Cursor(offset=int(row["offset"]), inode=int(row["inode"]), size=int(row["size"]))


def save_cursor(conn: sqlite3.Connection, path: str, cursor: Cursor, now: float) -> None:
    """Persist the read cursor for ``path``."""
    conn.execute(
        "INSERT INTO logwatch_cursor (path, offset, inode, size, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
        "  offset=excluded.offset, inode=excluded.inode, size=excluded.size, "
        "  updated_at=excluded.updated_at",
        (path, cursor.offset, cursor.inode, cursor.size, now),
    )


# --- Findings push (shared store) -------------------------------------------


def emit_finding(
    conn: sqlite3.Connection, finding: dict[str, Any], *, board: str = "",
    now: Optional[float] = None,
) -> None:
    """Upsert one open finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes title/detail/severity/evidence and ``updated_at`` but
    preserves ``created_at`` and never un-dismisses a finding a human resolved
    (dismissed/snoozed/accepted stay as-is) — the browse-only contract.
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


# --- Real wiring: entry point + CLI -----------------------------------------


def default_log_paths() -> list[Path]:
    """The watched logs under ``$HERMES_HOME/logs`` (fallback ~/.hermes/logs)."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    logs = home / "logs"
    return [logs / name for name in _DEFAULT_LOG_NAMES]


def open_store_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection, or None if the ledger is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching the other regular processes.
    """
    try:
        from hermes_cli import zeus_tokens
        path = zeus_tokens.default_zeus_db_path()
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _collect_sightings(
    conn: sqlite3.Connection, log_paths: Iterable[os.PathLike | str], *,
    min_level: str, from_start: bool, now: float,
) -> list[Sighting]:
    """Advance each log's cursor and gather this pass's anomaly sightings.

    First-attach default is *tail*: an untracked log is seeded to its current
    EOF and contributes nothing this pass, so activating the watcher never
    floods Проблемы with pre-existing history. ``from_start`` overrides that.
    """
    sightings: list[Sighting] = []
    for path in log_paths:
        key = str(path)
        cur = load_cursor(conn, key)
        if cur is None and not from_start:
            seed = eof_cursor(path)
            if seed is not None:
                save_cursor(conn, key, seed, now)
            continue
        lines, new_cur = read_new_lines(path, cur or Cursor())
        sightings.extend(extract_anomalies(lines, key, min_level))
        save_cursor(conn, key, new_cur, now)
    return sightings


def run_logwatch_scan(
    *,
    log_paths: Optional[Iterable[os.PathLike | str]] = None,
    board: str = "",
    threshold: int = DEFAULT_PROMOTE_THRESHOLD,
    min_level: str = DEFAULT_MIN_LEVEL,
    from_start: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    emit: bool = True,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """One watcher pass: tail the logs, gate signatures, push recurring findings.

    Returns the findings promoted this pass (empty when nothing recurred). A
    ``None`` connection with no zeus.db is a no-op (no store → no gate → no push).
    Callers may inject ``conn``/``log_paths`` for isolation in tests.
    """
    now = time.time() if now is None else now
    paths = list(default_log_paths() if log_paths is None else log_paths)
    own = conn is None
    if own:
        conn = open_store_db()
    if conn is None:
        return []
    try:
        ensure_schema(conn)
        prior = load_occurrences(conn)
        sightings = _collect_sightings(
            conn, paths, min_level=min_level, from_start=from_start, now=now,
        )
        records, findings = gate(prior, tally_sightings(sightings),
                                 threshold=threshold, now=now)
        save_occurrences(conn, records)
        if emit:
            for f in findings:
                emit_finding(conn, f, board=board, now=now)
        conn.commit()
        return findings
    finally:
        if own:
            conn.close()


def _render_human(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "Логвотчер: повторяющихся аномалий нет (разовый шум не поднимаем)."
    lines = [f"Логвотчер: {len(findings)} повторяющаяся(иеся) аномалия(и):"]
    for f in findings:
        lines.append(f"  [{f['severity']}] {f['title']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.logwatcher`` — the regular-process entry point."""
    parser = argparse.ArgumentParser(
        prog="logwatcher",
        description="Surface recurring log anomalies into Проблемы (browse-only).",
    )
    parser.add_argument("--board", default="",
                        help="Board slug for the findings (default: '' = global).")
    parser.add_argument("--threshold", type=int, default=DEFAULT_PROMOTE_THRESHOLD,
                        help="Sightings of one signature before it is promoted.")
    parser.add_argument("--min-level", default=DEFAULT_MIN_LEVEL,
                        help="Anomaly floor (DEBUG/INFO/WARNING/ERROR/CRITICAL).")
    parser.add_argument("--from-start", action="store_true",
                        help="Read logs from the beginning (default: tail from now).")
    parser.add_argument("--log", action="append", dest="logs",
                        help="Log file to watch (repeatable; default: standard set).")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute findings only; do not push to the store.")
    args = parser.parse_args(argv)

    findings = run_logwatch_scan(
        log_paths=[Path(os.path.expanduser(p)) for p in args.logs] if args.logs else None,
        board=args.board, threshold=args.threshold, min_level=args.min_level,
        from_start=args.from_start, emit=not args.no_emit,
    )
    if args.json:
        print(json.dumps({"finding_count": len(findings), "findings": findings},
                         ensure_ascii=False))
    else:
        print(_render_human(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
