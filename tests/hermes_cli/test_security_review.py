"""Tests for hermes_cli.security_review — the sealed-core regular security
review, its checklist, snapshot/diff regression flagging, and the sealed-cron
guard in cron.jobs.

The pure checks run on plain SecurityContext data (no host state). The snapshot
+ findings store uses an in-memory sqlite. The sealed-cron guard tests isolate
the cron store under a temp HERMES_HOME so nothing touches the live board or
the user's real cron jobs.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import security_review as sr


# ---------------------------------------------------------------------------
# Pure checks
# ---------------------------------------------------------------------------


def test_open_ports_flags_nonloopback():
    ctx = sr.SecurityContext(listeners=[
        {"name": "web", "host": "0.0.0.0", "port": 9119},
        {"name": "term", "host": "127.0.0.1", "port": 7000},
    ])
    r = sr.check_open_ports(ctx)
    assert r.status == sr.STATUS_FAIL and r.severity == "critical"
    assert r.evidence["exposed"][0]["name"] == "web"


def test_open_ports_ok_when_all_loopback():
    ctx = sr.SecurityContext(listeners=[
        {"name": "web", "host": "127.0.0.1", "port": 9119},
        {"name": "x", "host": "localhost", "port": 1},
        {"name": "y", "host": "::1", "port": 2},
    ])
    assert sr.check_open_ports(ctx).status == sr.STATUS_OK


def test_open_ports_skipped_without_probe():
    assert sr.check_open_ports(sr.SecurityContext()).status == sr.STATUS_SKIPPED


def test_secret_isolation_flags_global_scope():
    ctx = sr.SecurityContext(project_secrets=[
        {"name": "OPENAI_KEY", "scope": "global"},
        {"name": "P1_KEY", "scope": "project:p1"},
    ])
    r = sr.check_secret_isolation(ctx)
    assert r.status == sr.STATUS_FAIL
    assert r.evidence["leaky"][0]["name"] == "OPENAI_KEY"


def test_secret_isolation_ok_when_scoped():
    ctx = sr.SecurityContext(project_secrets=[{"name": "k", "scope": "project:p1"}])
    assert sr.check_secret_isolation(ctx).status == sr.STATUS_OK


def test_autonomy_policy_flags_unrestricted():
    assert sr.check_autonomy_policy(
        sr.SecurityContext(autonomy_policy={"mode": "unrestricted"})
    ).status == sr.STATUS_FAIL


def test_autonomy_policy_flags_no_approval():
    assert sr.check_autonomy_policy(
        sr.SecurityContext(autonomy_policy={"mode": "guarded", "requires_approval": False})
    ).status == sr.STATUS_FAIL


def test_autonomy_policy_ok_when_gated():
    assert sr.check_autonomy_policy(
        sr.SecurityContext(autonomy_policy={"mode": "guarded", "requires_approval": True})
    ).status == sr.STATUS_OK


def test_protected_paths_flags_missing_review_code():
    ctx = sr.SecurityContext(
        protected_paths=["cron/jobs.py"],
        required_protected=["hermes_cli/security_review.py", "cron/jobs.py"])
    r = sr.check_protected_paths(ctx)
    assert r.status == sr.STATUS_FAIL
    assert "hermes_cli/security_review.py" in r.evidence["missing"]


def test_protected_paths_covered_by_prefix():
    ctx = sr.SecurityContext(
        protected_paths=["hermes_cli"],
        required_protected=["hermes_cli/security_review.py"])
    assert sr.check_protected_paths(ctx).status == sr.STATUS_OK


def test_credential_staleness_warns_over_age():
    ctx = sr.SecurityContext(
        credentials=[{"name": "tok", "age_days": 120}], max_credential_age_days=90)
    r = sr.check_credential_staleness(ctx)
    assert r.status == sr.STATUS_WARN


def test_credential_staleness_ok_under_age():
    ctx = sr.SecurityContext(
        credentials=[{"name": "tok", "age_days": 10}], max_credential_age_days=90)
    assert sr.check_credential_staleness(ctx).status == sr.STATUS_OK


def test_backup_secret_exclusion_flags_leaked():
    ctx = sr.SecurityContext(
        backup_excludes={"foo"}, secret_exclude_required={".env", "auth.json"})
    r = sr.check_backup_secret_exclusion(ctx)
    assert r.status == sr.STATUS_FAIL
    assert set(r.evidence["leaked"]) == {".env", "auth.json"}


def test_backup_secret_exclusion_ok_when_excluded():
    ctx = sr.SecurityContext(
        backup_excludes={".env", "auth.json", "x"}, secret_exclude_required={".env"})
    assert sr.check_backup_secret_exclusion(ctx).status == sr.STATUS_OK


def test_dependency_vulns_flags_high():
    ctx = sr.SecurityContext(advisories=[
        {"package": "requests", "severity": "high"},
        {"package": "pip", "severity": "low"},
    ])
    r = sr.check_dependency_vulns(ctx)
    assert r.status == sr.STATUS_FAIL
    assert r.evidence["advisories"][0]["package"] == "requests"


def test_dependency_vulns_ok_when_only_low():
    ctx = sr.SecurityContext(advisories=[{"package": "pip", "severity": "low"}])
    assert sr.check_dependency_vulns(ctx).status == sr.STATUS_OK


def test_granted_permissions_flags_over_broad():
    ctx = sr.SecurityContext(permissions=[
        {"name": "fs", "granted_scope": "admin", "expected_scope": "read"},
        {"name": "net", "granted_scope": "read", "expected_scope": "read"},
    ])
    r = sr.check_granted_permissions(ctx)
    assert r.status == sr.STATUS_FAIL
    assert r.evidence["over_broad"][0]["name"] == "fs"


def test_granted_permissions_unknown_scope_flags_mismatch():
    ctx = sr.SecurityContext(permissions=[
        {"name": "x", "granted_scope": "weird", "expected_scope": "read"}])
    assert sr.check_granted_permissions(ctx).status == sr.STATUS_FAIL


def test_scope_exceeds_ladder():
    assert sr._scope_exceeds("admin", "read")
    assert not sr._scope_exceeds("read", "admin")
    assert not sr._scope_exceeds("read", None)


def test_run_checklist_runs_all_eight():
    results = sr.run_checklist(sr.SecurityContext())
    assert len(results) == len(sr.CHECKLIST) == 8
    # Bare context = every probe absent => all skipped, none crash.
    assert all(r.status == sr.STATUS_SKIPPED for r in results)


# ---------------------------------------------------------------------------
# Snapshot + findings store (in-memory sqlite — no host state)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _open_findings(conn, board=""):
    return conn.execute(
        "SELECT finding_key, title, status, severity FROM findings "
        "WHERE source='security' AND board=? ORDER BY finding_key", (board,)
    ).fetchall()


def test_snapshot_roundtrip_skips_skipped(conn):
    results = [
        sr.CheckResult(sr.CHECK_OPEN_PORTS, "t", sr.STATUS_FAIL, "critical", "bad"),
        sr.CheckResult(sr.CHECK_DEPENDENCY_VULNS, "t", sr.STATUS_SKIPPED, "info", "n/a"),
    ]
    sr.save_snapshot(conn, "", results)
    prev = sr.load_prev_snapshot(conn, "")
    assert prev[sr.CHECK_OPEN_PORTS]["status"] == sr.STATUS_FAIL
    assert sr.CHECK_DEPENDENCY_VULNS not in prev  # skipped never written


def test_first_run_failure_flags_regression(conn):
    results = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_FAIL,
                              "critical", "0.0.0.0")]
    emitted = sr.scan_and_emit({}, results, conn, board="b")
    assert emitted[0]["regressed"] is True
    rows = _open_findings(conn, "b")
    assert rows[0]["finding_key"] == "security:open_ports"
    assert rows[0]["title"].startswith("РЕГРЕССИЯ:")
    assert rows[0]["status"] == "open"


def test_persistent_hole_kept_but_not_regressed(conn):
    fail = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_FAIL, "critical", "x")]
    sr.scan_and_emit({}, fail, conn, board="b")            # first run: regression
    prev = sr.load_prev_snapshot(conn, "b")
    emitted = sr.scan_and_emit(prev, fail, conn, board="b")  # same fail again
    assert emitted[0]["regressed"] is False
    assert _open_findings(conn, "b")[0]["status"] == "open"  # still flagged


def test_recovery_clears_finding(conn):
    fail = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_FAIL, "critical", "x")]
    sr.scan_and_emit({}, fail, conn, board="b")
    prev = sr.load_prev_snapshot(conn, "b")
    ok = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_OK, "info", "clean")]
    sr.scan_and_emit(prev, ok, conn, board="b")
    assert _open_findings(conn, "b")[0]["status"] == "obsolete"


def test_skipped_current_preserves_baseline_and_finding(conn):
    fail = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_FAIL, "critical", "x")]
    sr.scan_and_emit({}, fail, conn, board="b")
    prev = sr.load_prev_snapshot(conn, "b")
    skipped = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_SKIPPED, "info", "n/a")]
    sr.scan_and_emit(prev, skipped, conn, board="b")
    # A probe gap must not falsely "resolve" a real hole.
    assert _open_findings(conn, "b")[0]["status"] == "open"
    assert sr.load_prev_snapshot(conn, "b")[sr.CHECK_OPEN_PORTS]["status"] == sr.STATUS_FAIL


def test_dismissed_finding_not_reopened(conn):
    fail = [sr.CheckResult(sr.CHECK_OPEN_PORTS, "Порты", sr.STATUS_FAIL, "critical", "x")]
    sr.scan_and_emit({}, fail, conn, board="b")
    conn.execute("UPDATE findings SET status='dismissed' WHERE source='security'")
    conn.commit()
    prev = sr.load_prev_snapshot(conn, "b")
    sr.scan_and_emit(prev, fail, conn, board="b")
    assert _open_findings(conn, "b")[0]["status"] == "dismissed"


def test_run_security_review_end_to_end(conn):
    ctx = sr.SecurityContext(listeners=[{"name": "web", "host": "0.0.0.0", "port": 1}])
    results = sr.run_security_review(board="b", ctx=ctx, conn=conn)
    assert any(r.check_id == sr.CHECK_OPEN_PORTS and r.status == sr.STATUS_FAIL
               for r in results)
    assert _open_findings(conn, "b")[0]["finding_key"] == "security:open_ports"


def test_run_security_review_no_conn_is_noop(monkeypatch):
    # Simulate a host without a zeus ledger so we never touch the real zeus.db:
    # compute-only, no crash, returns all results.
    monkeypatch.setattr(sr, "open_findings_db", lambda: None)
    ctx = sr.SecurityContext(listeners=[{"name": "w", "host": "0.0.0.0", "port": 1}])
    results = sr.run_security_review(board="b", ctx=ctx, conn=None, emit=True)
    assert len(results) == 8


# ---------------------------------------------------------------------------
# Sealed cron guard (isolated cron store)
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    return home


def test_sealed_job_cannot_be_paused(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="review", schedule="every 1d", sealed=True)
    assert job.get("sealed") is True
    with pytest.raises(jobs.SealedJobError):
        jobs.pause_job(job["id"])
    # Still enabled after the refused pause.
    assert jobs.get_job(job["id"]).get("enabled", True) is True


def test_sealed_job_cannot_be_removed(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="review", schedule="every 1d", sealed=True)
    with pytest.raises(jobs.SealedJobError):
        jobs.remove_job(job["id"])
    assert jobs.get_job(job["id"]) is not None


def test_sealed_job_cannot_be_disabled_via_update(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="review", schedule="every 1d", sealed=True)
    with pytest.raises(jobs.SealedJobError):
        jobs.update_job(job["id"], {"enabled": False})
    with pytest.raises(jobs.SealedJobError):
        jobs.update_job(job["id"], {"state": "paused"})


def test_sealed_flag_is_immutable(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="review", schedule="every 1d", sealed=True)
    with pytest.raises(ValueError, match="sealed"):
        jobs.update_job(job["id"], {"sealed": False})


def test_sealed_job_allows_benign_update(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="review", schedule="every 1d", sealed=True)
    updated = jobs.update_job(job["id"], {"name": "renamed"})
    assert updated["name"] == "renamed" and updated.get("sealed") is True


def test_unsealed_job_can_be_paused_and_removed(hermes_env):
    import cron.jobs as jobs
    job = jobs.create_job(prompt="x", schedule="every 1d")
    assert job.get("sealed") is None
    assert jobs.pause_job(job["id"]) is not None
    assert jobs.remove_job(job["id"]) is True


# ---------------------------------------------------------------------------
# Sealed cron seeding
# ---------------------------------------------------------------------------


def test_ensure_security_review_job_is_idempotent(hermes_env):
    import cron.jobs as jobs
    first = sr.ensure_security_review_job()
    assert first is not None
    assert first.get("sealed") is True
    assert first["no_agent"] is True
    assert (first.get("origin") or {}).get("kind") == "sealed-security-review"
    # Runner script was written into HERMES_HOME/scripts.
    assert (hermes_env / "scripts" / sr._RUNNER_SCRIPT_NAME).exists()
    # Second call short-circuits to the same job (no duplicate).
    second = sr.ensure_security_review_job()
    assert second["id"] == first["id"]
    sealed = [j for j in jobs.load_jobs()
              if (j.get("origin") or {}).get("kind") == "sealed-security-review"]
    assert len(sealed) == 1
