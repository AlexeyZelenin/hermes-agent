"""Tests for the Zeus supervised-startup-doctor section of ``hermes doctor``.

Covers hermes_cli.doctor._check_startup_doctor and its helpers. Every case runs
against a throwaway ``$HERMES_HOME`` under tmp_path with stub bash scripts — the
real ``~/.hermes/zeus`` scripts, launchd, and :9119 are never touched (task
t_a462cb12).
"""

import contextlib
import io

import hermes_cli.doctor as doctor

# Stub Zeus scripts driven by sidecar files in the same dir, so a test can steer
# `check`/`status`/`preflight` outcomes without rewriting the stub.
HEAL_STUB = """#!/bin/bash
d="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  check)     cat "$d/sigs" 2>/dev/null ;;
  preflight) rm -f "$d/sigs" ;;   # simulate a successful heal
esac
exit 0
"""

BOOT_STUB = """#!/bin/bash
d="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  status) cat "$d/statusline" 2>/dev/null || echo "gateway: healthy" ;;
esac
exit 0
"""


def _install(tmp_path, monkeypatch, *, sigs="", status_line="gateway: healthy (all pass)",
             pending_reload=None):
    home = tmp_path / "home"
    zeus = home / "zeus"
    zeus.mkdir(parents=True)
    (zeus / "heal_kanban.sh").write_text(HEAL_STUB)
    (zeus / "boot_supervisor.sh").write_text(BOOT_STUB)
    if sigs:
        (zeus / "sigs").write_text(sigs)
    (zeus / "statusline").write_text(status_line)
    import json
    (home / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "pending_reload": pending_reload})
    )
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: str(home))
    return home


def _run(should_fix=False):
    issues: list[str] = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor._check_startup_doctor(issues, should_fix)
    return issues, buf.getvalue()


class TestStartupDoctorSection:
    def test_noop_when_scripts_absent(self, tmp_path, monkeypatch):
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: str(tmp_path))
        issues, out = _run()
        assert issues == []
        assert "Supervised Startup Doctor" not in out

    def test_clean_and_healthy(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch)
        issues, out = _run()
        assert "Pre-flight: all 6 checks pass" in out
        assert "healthy" in out
        assert "No pending self-redeploy" in out
        assert issues == []

    def test_self_redeploy_pending_from_gateway_state(self, tmp_path, monkeypatch):
        _install(
            tmp_path, monkeypatch,
            pending_reload={"boot_rev": "f2f139bbd9", "disk_rev": "a5cb583f79"},
        )
        _issues, out = _run()
        assert "Self-redeploy pending" in out
        assert "f2f139bbd9" in out and "a5cb583f79" in out

    def test_preflight_faults_without_fix_appends_issue(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, sigs="db_corrupt\nlow_disk\n")
        issues, out = _run(should_fix=False)
        assert "Pre-flight faults detected" in out
        assert "db_corrupt" in out and "low_disk" in out
        assert any("--fix" in i for i in issues)

    def test_preflight_faults_healed_with_fix(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, sigs="wal_orphan\n")
        issues, out = _run(should_fix=True)
        assert "healed" in out
        assert issues == []  # preflight stub cleared the sigs -> re-check clean

    def test_preflight_faults_persist_after_fix(self, tmp_path, monkeypatch):
        # A missing-keys fault has no deterministic cure: the stub keeps its sigs
        # even after `preflight`, so the re-check must still flag it.
        home = tmp_path / "home"
        zeus = home / "zeus"
        zeus.mkdir(parents=True)
        stubborn = HEAL_STUB.replace("preflight) rm -f \"$d/sigs\" ;;", "preflight) : ;;")
        (zeus / "heal_kanban.sh").write_text(stubborn)
        (zeus / "boot_supervisor.sh").write_text(BOOT_STUB)
        (zeus / "sigs").write_text("missing_keys\n")
        (zeus / "statusline").write_text("gateway: healthy")
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: str(home))
        issues, out = _run(should_fix=True)
        assert "remain after healing" in out
        assert any("manual fix" in i for i in issues)

    def test_smoke_unhealthy_fails_and_appends_issue(self, tmp_path, monkeypatch):
        _install(
            tmp_path, monkeypatch,
            status_line="gateway: UNHEALTHY (first failing check: http_down)",
        )
        issues, out = _run()
        assert "supervised smoke failed" in out.lower()
        assert any("smoke failed" in i.lower() for i in issues)

class TestZeusDoctorHelpers:
    def test_locate_returns_none_when_missing(self, tmp_path, monkeypatch):
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: str(tmp_path))
        assert doctor._zeus_doctor_script("heal_kanban.sh") is None

    def test_run_captures_output(self, tmp_path):
        script = tmp_path / "echo.sh"
        script.write_text("#!/bin/bash\necho hello $1\n")
        rc, out = doctor._run_zeus_doctor(script, "world")
        assert rc == 0
        assert out == "hello world"

    def test_run_times_out_gracefully(self, tmp_path):
        script = tmp_path / "slow.sh"
        script.write_text("#!/bin/bash\nsleep 5\n")
        rc, out = doctor._run_zeus_doctor(script, timeout=1)
        assert rc == 124
        assert "timed out" in out
