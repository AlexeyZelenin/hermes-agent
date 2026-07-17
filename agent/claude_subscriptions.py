"""Claude Code subscription pool: named CLAUDE_CONFIG_DIR logins with rotation.

Claude Code holds its login per config dir. Each subscription in the pool is a
named config dir (``~/.claude`` = ``default``, ``~/.claude-sub-<name>`` = the
others) that the operator logged into once, interactively. Metadata and runtime
state (cooling windows, leases, limit events) live in the zeus sidecar DB so
the executor, the dispatcher, and the dashboard all see one pool.

Selection strategy is ``spread``: use every subscription concurrently, fewest
active sessions first, least-recently-limited as the tie-break, honouring a
per-subscription concurrency cap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SUB_DIR_PREFIX = ".claude-sub-"
DEFAULT_SUB_NAME = "default"

# Marker embedded in kanban block reasons so the dispatcher can recognise
# "blocked because every subscription is cooling" and auto-unblock the task
# the moment any subscription recovers.
SUBSCRIPTIONS_EXHAUSTED_MARKER = "[claude-subscriptions-exhausted]"

# Claude's session quota window. Used as the cooldown fallback when a limit
# message carries no parseable reset time: back off until the next 5h boundary.
LIMIT_WINDOW_SECONDS = 5 * 60 * 60

_DEFAULT_MAX_CONCURRENCY = 4
_CAPACITY_WAIT_SECONDS = 600.0
_CAPACITY_POLL_SECONDS = 15.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_subscriptions (
    name TEXT PRIMARY KEY,
    config_dir TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    max_concurrency INTEGER NOT NULL DEFAULT 4,
    cooling_until REAL,
    last_limited_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subscription TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    reset_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sub_events_sub ON subscription_events(subscription, ts);

CREATE TABLE IF NOT EXISTS subscription_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_leases_sub ON subscription_leases(subscription);
"""


class NoSubscriptionAvailable(RuntimeError):
    """Every registered subscription is cooling (or at capacity past the wait)."""

    def __init__(self, message: str, earliest_recovery: Optional[float] = None):
        super().__init__(message)
        self.earliest_recovery = earliest_recovery


@dataclass(frozen=True)
class Lease:
    id: int
    name: str
    config_dir: str


def db_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "zeus" / "zeus.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Credentials per config dir
# ---------------------------------------------------------------------------

def keychain_service(config_dir: str) -> str:
    """macOS Keychain service name Claude Code uses for a config dir.

    The default dir uses the bare service name; any other CLAUDE_CONFIG_DIR
    gets a ``-<sha256(path)[:8]>`` suffix (verified against Claude Code 2.x).
    """
    resolved = str(Path(config_dir).expanduser())
    if resolved == str(Path.home() / ".claude"):
        return "Claude Code-credentials"
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _read_credentials_file(config_dir: str) -> Optional[Dict[str, Any]]:
    cred_path = Path(config_dir).expanduser() / ".credentials.json"
    if not cred_path.exists():
        return None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    return oauth if oauth.get("accessToken") else None


def _read_credentials_keychain(config_dir: str) -> Optional[Dict[str, Any]]:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", keychain_service(config_dir), "-w"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") or {}
    return oauth if oauth.get("accessToken") else None


def read_subscription_credentials(config_dir: str) -> Optional[Dict[str, Any]]:
    """OAuth payload (accessToken/refreshToken/expiresAt/…) for a config dir."""
    return _read_credentials_keychain(config_dir) or _read_credentials_file(config_dir)


def subscription_access_token(config_dir: str) -> Optional[str]:
    """The pocket's stored OAuth access token, for querying its usage API.

    Best-effort: an idle pocket's token may be expired (Claude Code refreshes
    lazily on session start), in which case the usage API returns 401 and the
    dashboard shows the window as unavailable rather than a stale number.
    """
    oauth = read_subscription_credentials(config_dir)
    token = (oauth or {}).get("accessToken")
    return token or None


# Keychain lookups shell out to `security`; cache login state briefly so the
# capacity-wait poll loop and the dashboard don't hammer the OS.
_LOGIN_CACHE_TTL_SECONDS = 30.0
_login_cache: Dict[str, tuple] = {}


def _credentials_live(oauth: Optional[Dict[str, Any]]) -> bool:
    """Whether a stored OAuth payload still counts as a usable login.

    Mere presence of a ``.credentials.json`` (or Keychain entry) is not proof
    of a live login: a logged-out pocket can leave a *stale* payload behind
    whose access token has expired. If the pool leases such a pocket the
    session dies with 'Authentication required' — the exact failure that
    blocked prior task attempts. So a pocket is logged in only when its access
    token is unexpired, OR it carries a refresh token Claude Code can use to
    renew it on session start (the executor's ``is_auth_error`` rotation is the
    backstop for a refresh token that itself has been revoked).
    """
    if not oauth or not oauth.get("accessToken"):
        return False
    try:
        from agent.anthropic_adapter import is_claude_code_token_valid
    except Exception:  # pragma: no cover - adapter import should not fail
        return True  # fail-open: we already know an access token is present
    if is_claude_code_token_valid(oauth):
        return True
    return bool(oauth.get("refreshToken"))


def is_logged_in(config_dir: str) -> bool:
    cached = _login_cache.get(config_dir)
    now = time.monotonic()
    if cached and now - cached[0] < _LOGIN_CACHE_TTL_SECONDS:
        return cached[1]
    result = _credentials_live(read_subscription_credentials(config_dir))
    _login_cache[config_dir] = (now, result)
    return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _discover_config_dirs() -> Dict[str, str]:
    """Map subscription name -> config dir for every login dir on disk."""
    home = Path.home()
    dirs: Dict[str, str] = {}
    default_dir = home / ".claude"
    if default_dir.is_dir():
        dirs[DEFAULT_SUB_NAME] = str(default_dir)
    for entry in sorted(home.glob(f"{SUB_DIR_PREFIX}*")):
        name = entry.name[len(SUB_DIR_PREFIX):].strip()
        if entry.is_dir() and name and name != DEFAULT_SUB_NAME:
            dirs[name] = str(entry)
    return dirs


def default_max_concurrency() -> int:
    raw = os.getenv("HERMES_CLAUDE_SUB_DEFAULT_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_MAX_CONCURRENCY
    except ValueError:
        value = _DEFAULT_MAX_CONCURRENCY
    return max(1, value)


def sync_registry(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Upsert discovered config dirs into the registry; return current rows.

    Metadata (display name, notes, caps) is preserved across syncs. Rows whose
    directory disappeared stay registered but report ``dir_exists=False`` from
    :func:`pool_status` — deleting a dir must not silently drop its history.
    """
    own = conn is None
    conn = conn or connect()
    try:
        now = time.time()
        with conn:
            for name, config_dir in _discover_config_dirs().items():
                conn.execute(
                    "INSERT INTO claude_subscriptions"
                    " (name, config_dir, max_concurrency, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET"
                    "   config_dir = excluded.config_dir,"
                    "   updated_at = excluded.updated_at",
                    (name, config_dir, default_max_concurrency(), now, now),
                )
        rows = conn.execute(
            "SELECT * FROM claude_subscriptions ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own:
            conn.close()


def update_subscription(name: str, **fields: Any) -> bool:
    """Update mutable metadata (display_name, notes, enabled, max_concurrency)."""
    allowed = {"display_name", "notes", "enabled", "max_concurrency"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "max_concurrency" in updates:
        updates["max_concurrency"] = max(1, int(updates["max_concurrency"]))
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    conn = connect()
    try:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with conn:
            cur = conn.execute(
                f"UPDATE claude_subscriptions SET {assignments}, updated_at = ?"
                " WHERE name = ?",
                (*updates.values(), time.time(), name),
            )
        return cur.rowcount == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Limit detection and cooldown
# ---------------------------------------------------------------------------

_LIMIT_PATTERNS = re.compile(
    r"hit your (?:usage|session|weekly|5-hour|five-hour) limit"
    r"|usage limit reached"
    r"|reached your (?:usage|session|weekly) limit"
    r"|(?:5-hour|five-hour|session|weekly) limit reached"
    r"|limit will reset",
    re.IGNORECASE,
)

# These matchers are the LAST-RESORT fallback, applied only to error text - an
# ACP request failure's exception message or the adapter's stderr - after the
# structural typed-error signal (ACPUsageLimitError / ACPAuthError raised by the
# ACP client) has already been checked. They are never run against a task's
# returned handoff, so there is deliberately no length cap: a genuine limit/auth
# death can surface as a verbose multi-line exception, and the old 600-char gate
# made those go undetected and parked the task as a capability failure even
# though the pool had free capacity.
def is_usage_limit_error(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(_LIMIT_PATTERNS.search(text))


_AUTH_PATTERNS = re.compile(
    r"authentication required"
    r"|not logged in"
    r"|please run /login"
    r"|oauth token (?:has )?(?:expired|been revoked)"
    r"|invalid (?:api key|bearer token)",
    re.IGNORECASE,
)


def is_auth_error(text: Optional[str]) -> bool:
    """A session death caused by a logged-out or revoked subscription pocket.

    Treated like a limit event by the executor: the pocket cools down and the
    task rotates to the next subscription instead of blocking. Error text only
    (see :func:`is_usage_limit_error`); no length cap.
    """
    if not text:
        return False
    return bool(_AUTH_PATTERNS.search(text))


def _parse_clock_reset(message: str, now: float) -> Optional[float]:
    match = re.search(
        r"resets?(?: at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        message, re.IGNORECASE,
    )
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    minute = int(match.group(2) or 0)
    local = datetime.fromtimestamp(now)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def parse_limit_reset(message: str, now: Optional[float] = None) -> Optional[float]:
    """Best-effort reset timestamp from a Claude usage-limit message."""
    now = now if now is not None else time.time()
    # "Claude AI usage limit reached|1712345678" (epoch after a pipe)
    match = re.search(r"\|\s*(\d{10,13})\b", message)
    if not match:
        # "resetsAt": 1712345678 / resets_at=1712345678
        match = re.search(r"resets?_?at\D{0,4}(\d{10,13})\b", message, re.IGNORECASE)
    if match:
        value = float(match.group(1))
        if value > 1e12:  # milliseconds
            value /= 1000.0
        return value if value > now else None
    # ISO timestamp anywhere in the message
    iso = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?", message)
    if iso:
        try:
            text = iso.group(0).replace("Z", "+00:00")
            value = datetime.fromisoformat(text).timestamp()
            return value if value > now else None
        except ValueError:
            pass
    return _parse_clock_reset(message, now)


def next_window_boundary(now: Optional[float] = None) -> float:
    """Next epoch-aligned 5h boundary — the fallback cooldown horizon."""
    now = now if now is not None else time.time()
    return (int(now) // LIMIT_WINDOW_SECONDS + 1) * LIMIT_WINDOW_SECONDS


def mark_limited(name: str, message: str, now: Optional[float] = None) -> float:
    """Put a subscription into cooldown after a usage-limit death.

    Returns the ``cooling_until`` timestamp (parsed reset time when the
    message carries one, else the next 5h window boundary).
    """
    now = now if now is not None else time.time()
    reset_at = parse_limit_reset(message, now) or next_window_boundary(now)
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE claude_subscriptions SET cooling_until = ?,"
                " last_limited_at = ?, updated_at = ? WHERE name = ?",
                (reset_at, now, now, name),
            )
            conn.execute(
                "INSERT INTO subscription_events (ts, subscription, kind, detail, reset_at)"
                " VALUES (?, ?, 'limit', ?, ?)",
                (now, name, message[:2000], reset_at),
            )
        return reset_at
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Leases and spread selection
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _cleanup_stale_leases(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, pid FROM subscription_leases").fetchall()
    dead = [row["id"] for row in rows if not _pid_alive(int(row["pid"]))]
    if dead:
        conn.executemany(
            "DELETE FROM subscription_leases WHERE id = ?", [(i,) for i in dead]
        )


def _selectable_rows(conn: sqlite3.Connection, now: float) -> List[sqlite3.Row]:
    """Enabled, logged-in, non-cooling subscriptions in spread order."""
    rows = conn.execute(
        "SELECT s.*, COUNT(l.id) AS active FROM claude_subscriptions s"
        " LEFT JOIN subscription_leases l ON l.subscription = s.name"
        " WHERE s.enabled = 1"
        " GROUP BY s.name"
        " ORDER BY active ASC, COALESCE(s.last_limited_at, 0) ASC, s.name ASC"
    ).fetchall()
    ready = []
    for row in rows:
        if row["cooling_until"] and float(row["cooling_until"]) > now:
            continue
        if not Path(row["config_dir"]).is_dir():
            continue
        if not is_logged_in(row["config_dir"]):
            continue
        ready.append(row)
    return ready


def pool_size() -> int:
    """Registered, enabled, logged-in subscriptions (ignores cooling state)."""
    conn = connect()
    try:
        sync_registry(conn)
        rows = conn.execute(
            "SELECT config_dir FROM claude_subscriptions WHERE enabled = 1"
        ).fetchall()
        return sum(1 for row in rows if is_logged_in(row["config_dir"]))
    finally:
        conn.close()


def pool_has_capacity(now: Optional[float] = None) -> bool:
    """True when at least one subscription can take a session right now."""
    now = now if now is not None else time.time()
    conn = connect()
    try:
        with conn:
            _cleanup_stale_leases(conn)
        return any(
            int(row["active"]) < max(1, int(row["max_concurrency"]))
            for row in _selectable_rows(conn, now)
        )
    finally:
        conn.close()


def _try_acquire(conn: sqlite3.Connection, task_id: str, now: float) -> Optional[Lease]:
    # Login checks shell out to the Keychain — resolve candidates BEFORE
    # taking the write lock, then recheck lease counts atomically inside it.
    candidates = _selectable_rows(conn, now)
    if not candidates:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        _cleanup_stale_leases(conn)
        counts = {
            row["subscription"]: int(row["n"])
            for row in conn.execute(
                "SELECT subscription, COUNT(*) AS n FROM subscription_leases"
                " GROUP BY subscription"
            )
        }
        for row in candidates:
            if counts.get(row["name"], 0) >= max(1, int(row["max_concurrency"])):
                continue
            cur = conn.execute(
                "INSERT INTO subscription_leases (subscription, task_id, pid, acquired_at)"
                " VALUES (?, ?, ?, ?)",
                (row["name"], task_id, os.getpid(), now),
            )
            conn.commit()
            return Lease(id=int(cur.lastrowid), name=row["name"],
                         config_dir=row["config_dir"])
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise


def _earliest_recovery(conn: sqlite3.Connection) -> Optional[float]:
    row = conn.execute(
        "SELECT MIN(cooling_until) AS t FROM claude_subscriptions"
        " WHERE enabled = 1 AND cooling_until IS NOT NULL"
    ).fetchone()
    return float(row["t"]) if row and row["t"] else None


def acquire(task_id: str = "", wait_seconds: Optional[float] = None) -> Lease:
    """Lease a subscription using the spread strategy.

    Blocks (polling) while all subscriptions are merely at their concurrency
    cap; raises :class:`NoSubscriptionAvailable` immediately when every
    subscription is cooling, or after ``wait_seconds`` of no free capacity.
    """
    if wait_seconds is None:
        raw = os.getenv("HERMES_CLAUDE_SUB_CAPACITY_WAIT_SECONDS", "").strip()
        wait_seconds = float(raw) if raw else _CAPACITY_WAIT_SECONDS
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        now = time.time()
        conn = connect()
        try:
            sync_registry(conn)
            lease = _try_acquire(conn, task_id, now)
            if lease is not None:
                return lease
            selectable = _selectable_rows(conn, now)
            earliest = _earliest_recovery(conn)
        finally:
            conn.close()
        if not selectable:
            when = (
                f" (earliest recovery {datetime.fromtimestamp(earliest):%Y-%m-%d %H:%M})"
                if earliest else ""
            )
            raise NoSubscriptionAvailable(
                f"{SUBSCRIPTIONS_EXHAUSTED_MARKER} all Claude Code subscriptions"
                f" are cooling after usage limits{when}",
                earliest_recovery=earliest,
            )
        if time.monotonic() >= deadline:
            raise NoSubscriptionAvailable(
                f"{SUBSCRIPTIONS_EXHAUSTED_MARKER} no Claude Code subscription"
                f" capacity freed up within {int(wait_seconds)}s",
                earliest_recovery=None,
            )
        time.sleep(_CAPACITY_POLL_SECONDS)


def release(lease: Lease) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM subscription_leases WHERE id = ?", (lease.id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Status for the dashboard
# ---------------------------------------------------------------------------

def _burn_rate_tokens_per_hour(conn: sqlite3.Connection, name: str, now: float) -> Optional[int]:
    try:
        row = conn.execute(
            "SELECT SUM(total_tokens) AS total FROM token_usage"
            " WHERE subscription = ? AND ts > ?",
            (name, now - 3600),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # ledger table/column not present yet (zeus plugin absent)
    return int(row["total"]) if row and row["total"] else 0


def pool_status() -> List[Dict[str, Any]]:
    """Full per-subscription view: registry + leases + cooling + burn rate."""
    now = time.time()
    conn = connect()
    try:
        sync_registry(conn)
        with conn:
            _cleanup_stale_leases(conn)
        rows = conn.execute(
            "SELECT s.*, COUNT(l.id) AS active FROM claude_subscriptions s"
            " LEFT JOIN subscription_leases l ON l.subscription = s.name"
            " GROUP BY s.name ORDER BY s.name"
        ).fetchall()
        status = []
        for row in rows:
            cooling_until = row["cooling_until"]
            cooling = bool(cooling_until and float(cooling_until) > now)
            status.append({
                "name": row["name"],
                "config_dir": row["config_dir"],
                "display_name": row["display_name"],
                "notes": row["notes"],
                "enabled": bool(row["enabled"]),
                "dir_exists": Path(row["config_dir"]).is_dir(),
                "logged_in": is_logged_in(row["config_dir"]),
                "active_sessions": int(row["active"]),
                "max_concurrency": int(row["max_concurrency"]),
                "cooling": cooling,
                "cooling_until": float(cooling_until) if cooling else None,
                "last_limited_at": (
                    float(row["last_limited_at"]) if row["last_limited_at"] else None
                ),
                "burn_rate_tokens_per_hour": _burn_rate_tokens_per_hour(
                    conn, row["name"], now
                ),
            })
        return status
    finally:
        conn.close()
