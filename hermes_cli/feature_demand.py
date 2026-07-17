"""Feature-demand advisor — surface an unbuilt feature many open cards wait on.

Operator model (task t_e6487a56): the watching agent should nudge the operator
when an *unbuilt* feature is needed by SEVERAL cards at once. The night-shift
example: the paused-flag card ``t_d553512c`` is needed by the rename card, the
old ON-HOLD cards, and talk-to-task — three open cards blocked on one feature is
a concrete argument to raise that feature's priority. Building it once unblocks
all of them.

This is a new *advisor signal*: it scans the open cards for references to (and
explicit dependency links on) another card that is not yet built, counts how
many distinct open cards are waiting on each such feature, and — above a small
threshold — pushes a benefit-framed finding to the shared zeus ``findings``
store (``source=feature-demand``), the same store
:mod:`hermes_cli.regular_crons` and :mod:`hermes_cli.integrity_agent` push to.
Only the demand-above-threshold pushes; the pull view (open the Advisor tab)
stays quiet. Findings are dismissible: a human who snoozes/dismisses one is not
re-nagged (the upsert preserves ``dismissed``/``snoozed``), and demand that
falls back below threshold auto-clears.

A "reference" is either channel:

* **mention** — the feature card's id (``t_xxxxxxxx``) appears in a waiter's
  title or body (matches the example: cards that mention ``t_d553512c``);
* **link** — an explicit ``task_links`` dependency (the waiter is a child of the
  feature), i.e. the board already knows the waiter is blocked on it.

Pure, dependency-injectable: :func:`compute_demand` takes plain dicts + link
pairs so it unit-tests without a live board or ledger; ``load_board_cards`` /
``run_feature_demand_scan`` wire the real kanban DB and zeus.db. Everything
degrades to a bare, side-effect-free scan when the ledger is absent.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from typing import Any, Iterable, Optional

FINDINGS_SOURCE = "feature-demand"

# A card is "open" (live on the board) unless it is finished or filed away.
OPEN_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}
)
# A feature is a *buildable target* — un-built and re-prioritizable — only while
# it has not started (``running`` = already being built; ``review`` = built,
# awaiting sign-off). ``paused`` is orthogonal to status, so a paused ``todo``
# card is still buildable and *should* surface: that is exactly the "raise
# priority / un-pause it, N cards wait" case.
BUILDABLE_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "blocked"})

# Kanban task ids are ``t_`` + hex (``secrets.token_hex`` → 8 chars today, older
# cards used 4). Bounded so a bare ``t_`` or prose never matches.
_TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{4,}\b")

# Several cards waiting is the signal. Conservative floor so the push stays rare.
DEFAULT_MIN_WAITERS = 2


# --- Reference extraction (pure) --------------------------------------------


def extract_mentions(text: Optional[str]) -> set[str]:
    """Task ids mentioned in a blob of card text (``t_xxxx`` tokens)."""
    if not text:
        return set()
    return set(_TASK_ID_RE.findall(text.lower()))


def _card_mentions(card: dict[str, Any]) -> set[str]:
    """Feature ids a card references by text, excluding the card's own id."""
    text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
    mentions = extract_mentions(text)
    mentions.discard(str(card.get("id") or ""))
    return mentions


# --- Demand computation (pure) ----------------------------------------------


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Russian count agreement: 1 карту / 2 карты / 5 карт."""
    if n % 100 in (11, 12, 13, 14):
        return many
    tail = n % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def compute_demand(
    cards: Iterable[dict[str, Any]],
    *,
    links: Iterable[tuple[str, str]] = (),
    min_waiters: int = DEFAULT_MIN_WAITERS,
) -> list[dict[str, Any]]:
    """Demand entries for every unbuilt feature ≥ ``min_waiters`` open cards wait on.

    ``cards`` are the open board cards as ``{id, title, body, status, priority,
    paused}`` dicts; ``links`` are ``(parent_id, child_id)`` dependency pairs (a
    child waits on its parent). A feature counts only when it is itself an open,
    buildable card on the board — a done/running feature is nothing to raise.
    Returns one entry per qualifying feature, richest demand first.
    """
    by_id = {str(c.get("id")): c for c in cards if c.get("id")}
    # feature_id -> {waiter_id: channel-set}
    waiters: dict[str, dict[str, set[str]]] = {}

    def _record(feature_id: str, waiter_id: str, channel: str) -> None:
        if feature_id == waiter_id or feature_id not in by_id or waiter_id not in by_id:
            return
        if str(by_id[feature_id].get("status")) not in BUILDABLE_STATUSES:
            return  # feature already built / in progress -> nothing to raise
        if str(by_id[waiter_id].get("status")) not in OPEN_STATUSES:
            return  # a finished card is no longer waiting
        waiters.setdefault(feature_id, {}).setdefault(waiter_id, set()).add(channel)

    for waiter_id, card in by_id.items():
        for feature_id in _card_mentions(card):
            _record(feature_id, waiter_id, "mention")
    for parent_id, child_id in links:
        _record(str(parent_id), str(child_id), "link")

    demand = [
        _demand_entry(by_id[fid], wmap, by_id)
        for fid, wmap in waiters.items()
        if len(wmap) >= min_waiters
    ]
    demand.sort(key=lambda d: (-d["waiter_count"], d["feature_id"]))
    return demand


def _demand_entry(
    feature: dict[str, Any],
    wmap: dict[str, set[str]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Shape one feature's demand: the finding-ready dict for :func:`emit_finding`."""
    fid = str(feature.get("id"))
    waiter_ids = sorted(wmap)
    n = len(waiter_ids)
    via = {
        "mention": sorted(w for w, ch in wmap.items() if "mention" in ch),
        "link": sorted(w for w, ch in wmap.items() if "link" in ch),
    }
    title = str(feature.get("title") or fid)
    priority = int(feature.get("priority") or 0)
    names = ", ".join(
        f"{w} «{by_id[w].get('title') or w}»" for w in waiter_ids[:5]
    )
    if n > 5:
        names += " …"
    return {
        "feature_id": fid,
        "finding_key": finding_key(fid),
        "title": (
            f"«{title}» разблокирует {n} {_ru_plural(n, 'карту', 'карты', 'карт')} "
            "— поднять приоритет?"
        ),
        "detail": (
            f"{n} открытых карт ждут невыстроенную фичу {fid} «{title}»: {names}. "
            f"Построив её раньше остальных, разблокируете все {n} разом. "
            f"Текущий приоритет фичи — {priority}."
        ),
        "category": "prioritization",
        "severity": "info",
        "waiter_count": n,
        "evidence": {
            "feature_id": fid,
            "waiter_ids": waiter_ids,
            "waiter_count": n,
            "priority": priority,
            "via": via,
        },
    }


def finding_key(feature_id: str) -> str:
    return f"feature-demand:{feature_id}"


# --- Findings store ----------------------------------------------------------
#
# Mirrors the zeus ``findings`` schema shared with hermes_cli.regular_crons and
# hermes_cli.integrity_agent (``IF NOT EXISTS`` so whichever regular process runs
# first creates it; the DDL is identical, so there is no divergence). Sources
# coexist by their ``source`` column. NOTE: this DDL is duplicated across the
# three modules by necessity — see the follow-up to extract a shared
# ``findings_store`` once they have all landed.
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


def emit_finding(
    conn: sqlite3.Connection,
    finding: dict[str, Any],
    *,
    board: str,
    now: Optional[float] = None,
) -> None:
    """Upsert one open finding, keyed by ``(board, source, finding_key)``.

    Re-emitting refreshes title/detail/severity and ``updated_at`` but preserves
    ``created_at`` and never un-dismisses a finding a human already put to rest
    (dismissed/snoozed stay as-is).
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
        "  status=CASE WHEN findings.status IN ('dismissed','snoozed') "
        "              THEN findings.status ELSE 'open' END",
        (board, FINDINGS_SOURCE, finding["finding_key"], finding["title"],
         finding["detail"], json.dumps(finding.get("evidence")),
         finding.get("category", ""), finding.get("severity", "info"), now, now),
    )
    conn.commit()


def _open_finding_keys(conn: sqlite3.Connection, *, board: str) -> set[str]:
    """Currently-open feature-demand finding keys on ``board`` ("" for None)."""
    try:
        rows = conn.execute(
            "SELECT finding_key FROM findings "
            "WHERE source=? AND board=? AND status='open'",
            (FINDINGS_SOURCE, board),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


def clear_finding(conn: sqlite3.Connection, *, board: str, finding_key: str,
                  now: Optional[float] = None) -> None:
    """Mark a previously-open feature-demand finding obsolete (demand faded)."""
    now = time.time() if now is None else now
    try:
        conn.execute(
            "UPDATE findings SET status='obsolete', updated_at=? "
            "WHERE source=? AND board=? AND finding_key=? AND status='open'",
            (now, FINDINGS_SOURCE, board, finding_key),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return  # no findings table yet -> nothing to clear


def scan_and_emit(
    demand: Iterable[dict[str, Any]],
    conn: Optional[sqlite3.Connection],
    *,
    board: str = "",
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Push each demand finding and clear any prior finding whose demand faded.

    Idempotent: run it every tick. Every open feature-demand finding whose key is
    not in this pass's demand is cleared (the feature got built, or demand fell
    below threshold). Returns the findings pushed this pass. ``conn is None`` (no
    zeus ledger) is a no-op — demand is still computable, just without the push.
    """
    demand = list(demand)
    if conn is None:
        return []
    active = {d["finding_key"] for d in demand}
    for d in demand:
        emit_finding(conn, d, board=board, now=now)
    for key in _open_finding_keys(conn, board=board):
        if key not in active:
            clear_finding(conn, board=board, finding_key=key, now=now)
    return demand


# --- Real wiring: board load -------------------------------------------------


def _card_to_dict(task: Any) -> dict[str, Any]:
    """Project a kanban ``Task`` (or dict) to the fields the detector uses."""
    get = task.get if isinstance(task, dict) else lambda k: getattr(task, k, None)
    return {
        "id": get("id"),
        "title": get("title"),
        "body": get("body"),
        "status": get("status"),
        "priority": get("priority"),
        "paused": get("paused"),
    }


def load_board_cards(
    board: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Open cards + dependency links from the kanban DB, detector-shaped.

    Degrades to ``([], [])`` if the kanban module/DB is unavailable, so a host
    without a board simply has nothing to scan.
    """
    try:
        from hermes_cli import kanban_db
    except Exception:
        return [], []
    try:
        conn = kanban_db.connect(board=board)
    except Exception:
        return [], []
    try:
        rows = kanban_db.list_tasks(conn)
        cards = [
            _card_to_dict(t) for t in rows
            if str(getattr(t, "status", "")) in OPEN_STATUSES
        ]
        links = _load_links(conn)
    except Exception:
        return [], []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return cards, links


def _load_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """All ``(parent_id, child_id)`` dependency pairs; ``[]`` if the table is absent."""
    try:
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["parent_id"], r["child_id"]) for r in rows]


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings, or ``None`` if the file is absent.

    Declines to create the ledger from scratch (a host without zeus has no push
    path), matching :func:`hermes_cli.integrity_agent.open_findings_db`.
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


def run_feature_demand_scan(
    *,
    board: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    emit: bool = True,
    now: Optional[float] = None,
    min_waiters: int = DEFAULT_MIN_WAITERS,
) -> list[dict[str, Any]]:
    """Scan ``board``'s open cards for cross-card feature demand and push findings.

    Wires the real kanban DB (open cards + links) and zeus.db. When ``emit`` and a
    zeus.db exist, findings are upserted (and faded ones cleared); otherwise the
    demand is just returned. Returns the demand entries for this pass.
    """
    cards, links = load_board_cards(board)
    demand = compute_demand(cards, links=links, min_waiters=min_waiters)
    if not emit:
        return demand
    own_conn = conn is None
    if own_conn:
        conn = open_findings_db()
    try:
        return scan_and_emit(demand, conn, board=board or "", now=now)
    finally:
        if own_conn and conn is not None:
            conn.close()


# --- CLI ---------------------------------------------------------------------


def _render_human(demand: list[dict[str, Any]]) -> str:
    if not demand:
        return "Feature-demand: ни одна невыстроенная фича не ждётся несколькими картами."
    lines = [f"Feature-demand: {len(demand)} фича(и) со спросом нескольких карт:"]
    for d in demand:
        lines.append(f"  [{d['waiter_count']}×] {d['feature_id']}: {d['title']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.feature_demand`` — the advisor-signal entry point."""
    parser = argparse.ArgumentParser(
        prog="feature-demand",
        description="Advisor signal: cross-card demand for an unbuilt feature.",
    )
    parser.add_argument("--board", default=None, help="Kanban board slug (default: current).")
    parser.add_argument("--min-waiters", type=int, default=DEFAULT_MIN_WAITERS,
                        help=f"Waiting-card threshold (default: {DEFAULT_MIN_WAITERS}).")
    parser.add_argument("--json", action="store_true", help="Emit demand as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute demand only; do not push to the zeus store.")
    args = parser.parse_args(argv)

    demand = run_feature_demand_scan(
        board=args.board, emit=not args.no_emit, min_waiters=max(1, args.min_waiters)
    )

    if args.json:
        print(json.dumps({"demand_count": len(demand), "demand": demand},
                         ensure_ascii=False))
    else:
        print(_render_human(demand))
    # Non-zero exit iff any demand is open, so a cron can gate a nudge on it.
    return 1 if demand else 0


if __name__ == "__main__":
    raise SystemExit(main())
