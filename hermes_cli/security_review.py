"""Security-review agent — a sealed-core regular process that re-runs the
project's security checklist on a schedule, diffs each result against the
previous run, and flags regressions into the shared zeus ``findings`` store.

Operator model (design t_9004742f): security is NOT a one-off. Edits (incl.
vibe-coded ones) land continuously and can quietly re-open holes, so a standing
process must re-check the whole surface on a rhythm and shout when something
that was safe last run is unsafe now. The operator's hard requirement: the user
must not be able to *easily* switch this review off. Two honest tiers:

* **owner dev-instance** — there is no absolute protection from the owner; we
  make disabling HIGH-FRICTION and logged. The review lives in the sealed-core
  code layer (:mod:`hermes_cli.system_layers`), its cron is marked ``sealed``
  (:func:`cron.jobs.pause_job` / ``remove_job`` refuse it and log+flag the
  attempt), and edits to the review's own code are themselves a protected-path
  finding.
* **product multi-user** — security is part of the SEALED CORE; the end user
  cannot remove it at all.

Crucially the review **runs independent of the self-modify toggle** — it never
calls :func:`hermes_cli.system_layers.is_self_modify_enabled`. That toggle
gates whether sealed-core *code* may be rewritten; it must never gate whether
the safety review *fires*. The safety switches are always on and the review
does not wait for a toggle.

The checklist (each a pure, dependency-injectable check):

1. **open ports / endpoints** — listeners must bind loopback (the web terminal
   must be ``127.0.0.1``, never ``0.0.0.0``).
2. **per-project secret isolation (F4)** — no secret in a global/shared scope.
3. **autonomy policy (F5)** — an autonomous-action policy exists and still
   requires approval (not ``unrestricted``).
4. **protected paths** — the required protected paths (incl. the review's own
   code) are covered by the protected-path set.
5. **credential staleness** — no credential older than the max age.
6. **backup secret-exclusion** — every secret pattern that must be excluded
   from backups actually is.
7. **dependency vulnerabilities** — no high/critical advisory is open.
8. **granted permissions** — no permission granted beyond its expected scope.

Each check returns a :class:`CheckResult` with a status of ``ok`` / ``warn`` /
``fail`` / ``skipped``. ``skipped`` means the probe had no data this run — we
NEVER claim safety we did not verify, and a skipped check leaves the previous
snapshot (and any open finding) untouched rather than falsely "recovering" it.

Results snapshot into ``zeus.db`` (table ``security_review_snapshots``). The
next run diffs against that snapshot: any check whose status got worse — or is
bad on the very first run, i.e. worse than a clean baseline — is a regression
and is upserted as a ``source=security`` finding. A check that verifiably
recovered (now ``ok``) clears its finding. The agent's own token spend is
attributable via the zeus ledger by its cron run-session prefix (see
:mod:`hermes_cli.regular_crons`, which classifies this cron under
``purpose=security``), so no separate accounting lives here.

Pure and dependency-injectable: the checks take a :class:`SecurityContext` of
plain data so they unit-test without any live host state; ``default_context``
wires what is cheaply and safely readable and leaves the rest ``skipped``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

FINDINGS_SOURCE = "security"

# --- Status vocabulary ------------------------------------------------------

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

# Severity worseness for the diff. ``skipped`` is neutral (rank 0) but is never
# written to the snapshot nor allowed to clear a finding — see scan_and_emit.
_STATUS_RANK = {STATUS_OK: 0, STATUS_SKIPPED: 0, STATUS_WARN: 1, STATUS_FAIL: 2}

# Check ids — stable strings; they form the finding_key, so renaming one
# orphans its prior findings and snapshot rows.
CHECK_OPEN_PORTS = "open_ports"
CHECK_SECRET_ISOLATION = "secret_isolation"
CHECK_AUTONOMY_POLICY = "autonomy_policy"
CHECK_PROTECTED_PATHS = "protected_paths"
CHECK_CREDENTIAL_STALENESS = "credential_staleness"
CHECK_BACKUP_SECRET_EXCLUSION = "backup_secret_exclusion"
CHECK_DEPENDENCY_VULNS = "dependency_vulns"
CHECK_GRANTED_PERMISSIONS = "granted_permissions"

# Hosts that count as loopback (only these are a safe bind for a local service).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

# Ordered scope ladder for the granted-permissions check.
_SCOPE_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3}

DEFAULT_MAX_CREDENTIAL_AGE_DAYS = 90


def status_rank(status: Optional[str]) -> int:
    """Worseness rank of a status (higher is worse); unknown -> 0."""
    return _STATUS_RANK.get(str(status or STATUS_OK), 0)


# --- Result + context shapes ------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One checklist item's outcome for a single run."""

    check_id: str
    title: str
    status: str
    severity: str = "info"
    detail: str = ""
    evidence: Any = None


@dataclass
class SecurityContext:
    """Injectable inputs for the checklist. Each ``None`` field means the probe
    produced no data this run, so its check reports ``skipped`` rather than
    guessing a verdict.

    * ``listeners``: ``[{"name","host","port"}, ...]``
    * ``project_secrets``: ``[{"name","scope","project"}, ...]``
    * ``autonomy_policy``: ``{"mode","requires_approval"}``
    * ``protected_paths`` + ``required_protected``: covered set vs must-cover
    * ``credentials``: ``[{"name","age_days"}, ...]`` (``max_credential_age_days``)
    * ``backup_excludes`` + ``secret_exclude_required``: excluded vs must-exclude
    * ``advisories``: ``[{"package","severity"}, ...]``
    * ``permissions``: ``[{"name","granted_scope","expected_scope"}, ...]``
    """

    listeners: Optional[list[dict[str, Any]]] = None
    project_secrets: Optional[list[dict[str, Any]]] = None
    autonomy_policy: Optional[dict[str, Any]] = None
    protected_paths: Optional[list[str]] = None
    required_protected: list[str] = field(default_factory=list)
    credentials: Optional[list[dict[str, Any]]] = None
    max_credential_age_days: int = DEFAULT_MAX_CREDENTIAL_AGE_DAYS
    backup_excludes: Optional[set[str]] = None
    secret_exclude_required: Optional[set[str]] = None
    advisories: Optional[list[dict[str, Any]]] = None
    permissions: Optional[list[dict[str, Any]]] = None


def _skip(check_id: str, title: str, why: str) -> CheckResult:
    return CheckResult(check_id, title, STATUS_SKIPPED, "info", why)


# --- Checklist (pure) -------------------------------------------------------


def check_open_ports(ctx: SecurityContext) -> CheckResult:
    """Every listener must bind loopback — a non-loopback bind is a hole."""
    title = "Открытые порты/эндпоинты"
    if ctx.listeners is None:
        return _skip(CHECK_OPEN_PORTS, title, "слушатели не проверялись")
    exposed = [
        dict(listener) for listener in ctx.listeners
        if str(listener.get("host") or "").strip().lower() not in _LOOPBACK_HOSTS
    ]
    if not exposed:
        return CheckResult(CHECK_OPEN_PORTS, title, STATUS_OK, "info",
                           "все слушатели на loopback")
    names = ", ".join(f"{e.get('name') or '?'}@{e.get('host')}:{e.get('port')}"
                      for e in exposed)
    return CheckResult(
        CHECK_OPEN_PORTS, title, STATUS_FAIL, "critical",
        f"слушатели не на loopback: {names}", {"exposed": exposed})


def check_secret_isolation(ctx: SecurityContext) -> CheckResult:
    """F4: no secret may sit in a global/shared scope — secrets are per-project."""
    title = "Изоляция секретов per project (F4)"
    if ctx.project_secrets is None:
        return _skip(CHECK_SECRET_ISOLATION, title, "секреты не проверялись")
    leaky = [dict(s) for s in ctx.project_secrets
             if str(s.get("scope") or "").strip().lower() in {"global", "shared"}]
    if not leaky:
        return CheckResult(CHECK_SECRET_ISOLATION, title, STATUS_OK, "info",
                           "все секреты изолированы по проектам")
    names = ", ".join(str(s.get("name") or "?") for s in leaky)
    return CheckResult(
        CHECK_SECRET_ISOLATION, title, STATUS_FAIL, "critical",
        f"секреты в общем скоупе: {names}", {"leaky": leaky})


def check_autonomy_policy(ctx: SecurityContext) -> CheckResult:
    """F5: an autonomy policy must exist and still gate actions behind approval."""
    title = "Policy автономии (F5)"
    policy = ctx.autonomy_policy
    if policy is None:
        return _skip(CHECK_AUTONOMY_POLICY, title, "policy не проверялась")
    mode = str(policy.get("mode") or "").strip().lower()
    requires_approval = bool(policy.get("requires_approval", True))
    if mode == "unrestricted" or not requires_approval:
        return CheckResult(
            CHECK_AUTONOMY_POLICY, title, STATUS_FAIL, "warning",
            f"политика автономии не требует апрува (mode={mode or 'n/a'})",
            {"mode": mode, "requires_approval": requires_approval})
    return CheckResult(CHECK_AUTONOMY_POLICY, title, STATUS_OK, "info",
                       f"апрув требуется (mode={mode or 'default'})")


def check_protected_paths(ctx: SecurityContext) -> CheckResult:
    """The required protected paths (incl. the review's own code) must be covered."""
    title = "Protected paths"
    if ctx.protected_paths is None:
        return _skip(CHECK_PROTECTED_PATHS, title, "protected paths не проверялись")
    covered = list(ctx.protected_paths)
    missing = [p for p in ctx.required_protected if not _path_covered(p, covered)]
    if not missing:
        return CheckResult(CHECK_PROTECTED_PATHS, title, STATUS_OK, "info",
                           "все обязательные пути под защитой")
    return CheckResult(
        CHECK_PROTECTED_PATHS, title, STATUS_FAIL, "warning",
        f"не под защитой: {', '.join(missing)}", {"missing": missing})


def _path_covered(path: str, covered: Iterable[str]) -> bool:
    """True if ``path`` is exactly listed or lives under a covered prefix."""
    p = path.strip("/")
    for entry in covered:
        e = str(entry).strip("/")
        if p == e or p.startswith(e + "/"):
            return True
    return False


def check_credential_staleness(ctx: SecurityContext) -> CheckResult:
    """No credential may be older than the configured max age."""
    title = "Staleness кредов"
    if ctx.credentials is None:
        return _skip(CHECK_CREDENTIAL_STALENESS, title, "креды не проверялись")
    limit = ctx.max_credential_age_days
    stale = [dict(c) for c in ctx.credentials if (c.get("age_days") or 0) > limit]
    if not stale:
        return CheckResult(CHECK_CREDENTIAL_STALENESS, title, STATUS_OK, "info",
                           f"нет кредов старше {limit}д")
    names = ", ".join(f"{c.get('name') or '?'}({int(c.get('age_days') or 0)}д)"
                      for c in stale)
    return CheckResult(
        CHECK_CREDENTIAL_STALENESS, title, STATUS_WARN, "warning",
        f"устаревшие креды (>{limit}д): {names}", {"stale": stale})


def check_backup_secret_exclusion(ctx: SecurityContext) -> CheckResult:
    """Every secret pattern that must be excluded from backups actually is."""
    title = "Бэкап-исключения секретов"
    if ctx.backup_excludes is None or ctx.secret_exclude_required is None:
        return _skip(CHECK_BACKUP_SECRET_EXCLUSION, title,
                     "правила бэкапа не проверялись")
    leaked = sorted(ctx.secret_exclude_required - set(ctx.backup_excludes))
    if not leaked:
        return CheckResult(CHECK_BACKUP_SECRET_EXCLUSION, title, STATUS_OK, "info",
                           "секреты исключены из бэкапа")
    return CheckResult(
        CHECK_BACKUP_SECRET_EXCLUSION, title, STATUS_FAIL, "critical",
        f"секреты не исключены из бэкапа: {', '.join(leaked)}",
        {"leaked": leaked})


def check_dependency_vulns(ctx: SecurityContext) -> CheckResult:
    """No high/critical dependency advisory may be open."""
    title = "Уязвимости зависимостей"
    if ctx.advisories is None:
        return _skip(CHECK_DEPENDENCY_VULNS, title, "зависимости не сканировались")
    bad = [dict(a) for a in ctx.advisories
           if str(a.get("severity") or "").strip().lower() in {"high", "critical"}]
    if not bad:
        return CheckResult(CHECK_DEPENDENCY_VULNS, title, STATUS_OK, "info",
                           "нет high/critical уязвимостей")
    names = ", ".join(f"{a.get('package') or '?'}({a.get('severity')})" for a in bad)
    return CheckResult(
        CHECK_DEPENDENCY_VULNS, title, STATUS_FAIL, "warning",
        f"уязвимые зависимости: {names}", {"advisories": bad})


def check_granted_permissions(ctx: SecurityContext) -> CheckResult:
    """No permission may be granted beyond the scope it is expected to have."""
    title = "Granted-права"
    if ctx.permissions is None:
        return _skip(CHECK_GRANTED_PERMISSIONS, title, "права не проверялись")
    over = [dict(p) for p in ctx.permissions if _scope_exceeds(
        p.get("granted_scope"), p.get("expected_scope"))]
    if not over:
        return CheckResult(CHECK_GRANTED_PERMISSIONS, title, STATUS_OK, "info",
                           "нет прав шире ожидаемого")
    names = ", ".join(
        f"{p.get('name') or '?'}({p.get('granted_scope')}>{p.get('expected_scope')})"
        for p in over)
    return CheckResult(
        CHECK_GRANTED_PERMISSIONS, title, STATUS_FAIL, "warning",
        f"права шире ожидаемого: {names}", {"over_broad": over})


def _scope_exceeds(granted: Any, expected: Any) -> bool:
    """True if ``granted`` sits above ``expected`` on the scope ladder.

    Unknown scope strings can't be ranked, so they're only over-broad when they
    differ from ``expected`` — a conservative "unrecognised grant" flag.
    """
    if expected is None:
        return False
    g, e = str(granted or "").lower(), str(expected).lower()
    if g in _SCOPE_RANK and e in _SCOPE_RANK:
        return _SCOPE_RANK[g] > _SCOPE_RANK[e]
    return g != e


# Ordered registry: (check_id, fn). Order is the render/scan order.
CHECKLIST: tuple[tuple[str, Callable[[SecurityContext], CheckResult]], ...] = (
    (CHECK_OPEN_PORTS, check_open_ports),
    (CHECK_SECRET_ISOLATION, check_secret_isolation),
    (CHECK_AUTONOMY_POLICY, check_autonomy_policy),
    (CHECK_PROTECTED_PATHS, check_protected_paths),
    (CHECK_CREDENTIAL_STALENESS, check_credential_staleness),
    (CHECK_BACKUP_SECRET_EXCLUSION, check_backup_secret_exclusion),
    (CHECK_DEPENDENCY_VULNS, check_dependency_vulns),
    (CHECK_GRANTED_PERMISSIONS, check_granted_permissions),
)


def run_checklist(ctx: SecurityContext) -> list[CheckResult]:
    """Run every check once; a check that raises degrades to ``skipped``."""
    results: list[CheckResult] = []
    for check_id, fn in CHECKLIST:
        try:
            results.append(fn(ctx))
        except Exception as exc:  # a broken probe must not abort the whole run
            logger.debug("security check %s failed: %s", check_id, exc)
            results.append(_skip(check_id, check_id, f"проверка упала: {exc}"))
    return results


# --- Snapshot store (zeus.db) -----------------------------------------------

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS security_review_snapshots (
    board         TEXT NOT NULL DEFAULT '',
    check_id      TEXT NOT NULL,
    status        TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'info',
    detail        TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT 'null',
    updated_at    REAL NOT NULL,
    UNIQUE(board, check_id)
);
"""


def load_prev_snapshot(conn: sqlite3.Connection, board: str) -> dict[str, dict[str, Any]]:
    """Prior run's result per check_id, or ``{}`` before the first run."""
    conn.execute(_SNAPSHOT_SCHEMA)
    rows = conn.execute(
        "SELECT check_id, status, severity, detail, evidence_json "
        "FROM security_review_snapshots WHERE board=?", (board,)).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["check_id"]] = {
            "status": row["status"], "severity": row["severity"],
            "detail": row["detail"], "evidence": _loads(row["evidence_json"]),
        }
    return out


def save_snapshot(conn: sqlite3.Connection, board: str,
                  results: Iterable[CheckResult], now: Optional[float] = None) -> None:
    """Upsert the current run's results. ``skipped`` checks are NOT written, so
    a probe gap preserves the last known baseline instead of erasing it."""
    now = time.time() if now is None else now
    conn.execute(_SNAPSHOT_SCHEMA)
    for r in results:
        if r.status == STATUS_SKIPPED:
            continue
        conn.execute(
            "INSERT INTO security_review_snapshots "
            "(board, check_id, status, severity, detail, evidence_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(board, check_id) DO UPDATE SET "
            "  status=excluded.status, severity=excluded.severity, "
            "  detail=excluded.detail, evidence_json=excluded.evidence_json, "
            "  updated_at=excluded.updated_at",
            (board, r.check_id, r.status, r.severity, r.detail,
             json.dumps(r.evidence), now))
    conn.commit()


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


# --- Findings store ---------------------------------------------------------
#
# Mirrors the shared zeus ``findings`` schema (see hermes_cli.integrity_agent /
# hermes_cli.regular_crons). Same DDL, distinguished by source=security.
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


def finding_key(check_id: str) -> str:
    return f"security:{check_id}"


def emit_finding(conn: sqlite3.Connection, result: CheckResult, *, board: str,
                 regressed: bool, prev_status: str, now: Optional[float] = None) -> None:
    """Upsert one open security finding for a currently-bad check.

    Re-emitting refreshes title/detail/severity + ``updated_at`` but preserves
    ``created_at`` and never un-dismisses a finding a human already put to rest.
    """
    now = time.time() if now is None else now
    conn.execute(_FINDINGS_SCHEMA)
    prefix = "РЕГРЕССИЯ: " if regressed else ""
    title = f"{prefix}{result.title}"
    evidence = {"check_id": result.check_id, "status": result.status,
                "prev_status": prev_status, "regressed": regressed,
                "detail": result.detail, "evidence": result.evidence}
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
        (board, FINDINGS_SOURCE, finding_key(result.check_id), title,
         result.detail, json.dumps(evidence), "security", result.severity, now, now))
    conn.commit()


def clear_finding(conn: sqlite3.Connection, *, board: str, check_id: str,
                  now: Optional[float] = None) -> None:
    """Mark a previously-open security finding obsolete (check recovered)."""
    now = time.time() if now is None else now
    try:
        conn.execute(
            "UPDATE findings SET status='obsolete', updated_at=? "
            "WHERE source=? AND board=? AND finding_key=? AND status='open'",
            (now, FINDINGS_SOURCE, board, finding_key(check_id)))
        conn.commit()
    except sqlite3.OperationalError:
        return  # no findings table yet -> nothing to clear


def scan_and_emit(prev: dict[str, dict[str, Any]], results: Iterable[CheckResult],
                  conn: Optional[sqlite3.Connection], *, board: str = "",
                  now: Optional[float] = None) -> list[dict[str, Any]]:
    """Flag every currently-bad check (marking regressions vs ``prev``), clear
    findings for verifiably-recovered checks, then persist this run's snapshot.

    A check is a *regression* when its status is worse than the previous run's —
    a missing prior counts as a clean baseline, so a hole open on the very first
    run flags immediately (safety switches are always on, never silent). A check
    that is bad but no worse than before still keeps its finding open. ``skipped``
    checks touch nothing: no flag, no clear, no snapshot overwrite. ``conn is
    None`` (no zeus ledger) is a no-op push path.
    """
    results = list(results)
    if conn is None:
        return []
    emitted: list[dict[str, Any]] = []
    for r in results:
        if r.status == STATUS_SKIPPED:
            continue
        if r.status == STATUS_OK:
            clear_finding(conn, board=board, check_id=r.check_id, now=now)
            continue
        prev_status = str(prev.get(r.check_id, {}).get("status") or STATUS_OK)
        regressed = status_rank(r.status) > status_rank(prev_status)
        emit_finding(conn, r, board=board, regressed=regressed,
                     prev_status=prev_status, now=now)
        emitted.append({"check_id": r.check_id, "status": r.status,
                        "severity": r.severity, "regressed": regressed})
    save_snapshot(conn, board, results, now=now)
    return emitted


# --- Real wiring ------------------------------------------------------------


def default_context() -> SecurityContext:
    """Build a context from what is cheaply and safely readable right now.

    Probes that require live runtime state or config we can't verify are left
    ``None`` (their checks report ``skipped``) rather than asserting a verdict —
    honesty over false green. ``required_protected`` always includes the
    review's own code so an unprotected review surfaces as a finding once a
    protected-path probe is wired.
    """
    ctx = SecurityContext(required_protected=[
        "hermes_cli/security_review.py", "cron/jobs.py",
    ])
    try:
        from hermes_cli import backup as _backup
        ctx.backup_excludes = set(getattr(_backup, "_EXCLUDED_NAMES", set()))
    except Exception as exc:
        logger.debug("backup-exclude probe unavailable: %s", exc)
    return ctx


def open_findings_db() -> Optional[sqlite3.Connection]:
    """Writable zeus.db connection for findings + snapshots, or ``None`` if the
    file is absent (a host without zeus has no push path). Mirrors
    :func:`hermes_cli.integrity_agent.open_findings_db`."""
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


def run_security_review(*, board: str = "", ctx: Optional[SecurityContext] = None,
                        conn: Optional[sqlite3.Connection] = None, emit: bool = True,
                        now: Optional[float] = None) -> list[CheckResult]:
    """Run the checklist, diff against the prior snapshot, flag regressions.

    Runs UNCONDITIONALLY — it never consults the self-modify toggle; the safety
    review must fire regardless of whether sealed-core edits are unlocked. When
    ``emit`` and a zeus.db exist, regressions are upserted and recovered checks
    cleared; otherwise the results are just returned. Returns this run's results.
    """
    ctx = ctx if ctx is not None else default_context()
    results = run_checklist(ctx)
    if not emit:
        return results
    own_conn = conn is None
    if own_conn:
        conn = open_findings_db()
    if conn is None:
        return results
    try:
        prev = load_prev_snapshot(conn, board)
        scan_and_emit(prev, results, conn, board=board, now=now)
    finally:
        if own_conn:
            conn.close()
    return results


# --- Sealed cron seeding ----------------------------------------------------

SEALED_JOB_ORIGIN = {"kind": "sealed-security-review"}
_RUNNER_SCRIPT_NAME = "security_review_cron.py"
_RUNNER_SCRIPT_BODY = (
    "# Auto-generated by hermes_cli.security_review — sealed security-review\n"
    "# cron runner (task t_fd081437). Managed by the sealed core; do not edit.\n"
    "from hermes_cli.security_review import main\n"
    "raise SystemExit(main())\n"
)
_DEFAULT_SCHEDULE = "0 3 * * *"  # daily 03:00 (calendar cadence)


def _write_runner_script() -> Optional[str]:
    """Write the thin runner into ``HERMES_HOME/scripts`` and return its name."""
    try:
        from hermes_constants import get_hermes_home
        scripts_dir = get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / _RUNNER_SCRIPT_NAME).write_text(
            _RUNNER_SCRIPT_BODY, encoding="utf-8")
        return _RUNNER_SCRIPT_NAME
    except Exception as exc:
        logger.debug("could not write security-review runner: %s", exc)
        return None


def ensure_security_review_job(*, schedule: str = _DEFAULT_SCHEDULE
                               ) -> Optional[dict[str, Any]]:
    """Idempotently register the sealed security-review cron.

    Runs the checklist runner as a ``no_agent`` script job on a daily calendar
    schedule, marked ``sealed`` so the user cannot disable/pause/remove it (see
    :class:`cron.jobs.SealedJobError`). Safe to call on every boot/tick — an
    already-registered job short-circuits. Returns the existing or newly created
    job, or ``None`` when the cron store is unavailable.
    """
    try:
        from cron import jobs as cron_jobs
    except Exception:
        return None
    try:
        for job in cron_jobs.load_jobs():
            if (job.get("origin") or {}).get("kind") == SEALED_JOB_ORIGIN["kind"]:
                return job
        script = _write_runner_script()
        if script is None:
            return None
        return cron_jobs.create_job(
            prompt=None, schedule=schedule, name="Регулярное security-ревью",
            script=script, no_agent=True, deliver="local",
            origin=dict(SEALED_JOB_ORIGIN), sealed=True)
    except Exception as exc:
        logger.debug("could not ensure security-review cron: %s", exc)
        return None


# --- CLI --------------------------------------------------------------------


def _render_human(results: list[CheckResult]) -> str:
    bad = [r for r in results if r.status in (STATUS_WARN, STATUS_FAIL)]
    checked = sum(1 for r in results if r.status != STATUS_SKIPPED)
    skipped = len(results) - checked
    coverage = f"{checked} проверено, {skipped} не проверялось"
    if not bad:
        return f"Security: дыр не найдено ({coverage})."
    lines = [f"Security: {len(bad)} проблема(ы) ({coverage}):"]
    for r in bad:
        lines.append(f"  [{r.severity}] {r.check_id}: {r.detail}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.security_review`` — the sealed regular-process entry."""
    parser = argparse.ArgumentParser(
        prog="security-review",
        description="Sealed-core regular security checklist + regression flagging.")
    parser.add_argument("--board", default="", help="Kanban board slug.")
    parser.add_argument("--json", action="store_true", help="Emit results as JSON.")
    parser.add_argument("--no-emit", action="store_true",
                        help="Compute results only; do not push to the zeus store.")
    args = parser.parse_args(argv)

    results = run_security_review(board=args.board, emit=not args.no_emit)
    if args.json:
        print(json.dumps(
            {"results": [r.__dict__ for r in results]}, ensure_ascii=False))
    else:
        print(_render_human(results))
    # Non-zero exit iff any fail-level hole is open, so a cron can alert.
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
