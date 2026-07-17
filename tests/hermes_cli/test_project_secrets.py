"""Project-scoped credential isolation (hermes_cli/project_secrets + spawn).

Acceptance target (task t_3d3f8c94): tests confirm STRICT secret isolation
between at least two projects - correct resolution, no cross-project access,
and safe failure on a missing secret.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import project_secrets as psec


@pytest.fixture
def store(tmp_path):
    """A file store rooted at an isolated per-test HERMES_HOME."""
    return psec.FileProjectSecretStore(home=tmp_path / ".hermes")


# ---------------------------------------------------------------------------
# Store: roundtrip, isolation, safe absence
# ---------------------------------------------------------------------------


def test_set_get_roundtrip(store):
    store.set("p_alpha1", "ANTHROPIC_API_KEY", "sk-alpha")
    assert store.get("p_alpha1", "ANTHROPIC_API_KEY") == "sk-alpha"
    assert store.get_all("p_alpha1") == {"ANTHROPIC_API_KEY": "sk-alpha"}


def test_missing_secret_returns_default_not_another_projects(store):
    store.set("p_alpha1", "OPENAI_API_KEY", "sk-a")
    store.set("p_beta22", "OPENAI_API_KEY", "sk-b")
    # A key absent from a project resolves to the default - never a peer's value.
    assert store.get("p_alpha1", "GROQ_API_KEY") is None
    assert store.get("p_alpha1", "GROQ_API_KEY", "fallback") == "fallback"
    # And a present key never bleeds across projects.
    assert store.get("p_alpha1", "OPENAI_API_KEY") == "sk-a"
    assert store.get("p_beta22", "OPENAI_API_KEY") == "sk-b"


def test_two_projects_are_strictly_isolated(store):
    store.set("p_alpha1", "PROVIDER_KEY", "value-A")
    store.set("p_beta22", "PROVIDER_KEY", "value-B")
    a = store.get_all("p_alpha1")
    b = store.get_all("p_beta22")
    assert a == {"PROVIDER_KEY": "value-A"}
    assert b == {"PROVIDER_KEY": "value-B"}
    assert "value-B" not in a.values()
    assert "value-A" not in b.values()


def test_delete(store):
    store.set("p_alpha1", "TOKEN_X", "v")
    assert store.delete("p_alpha1", "TOKEN_X") is True
    assert store.delete("p_alpha1", "TOKEN_X") is False
    assert store.get("p_alpha1", "TOKEN_X") is None


def test_names_lists_names_only(store):
    store.set("p_alpha1", "B_KEY", "secret-b")
    store.set("p_alpha1", "A_KEY", "secret-a")
    names = store.names("p_alpha1")
    assert names == ["A_KEY", "B_KEY"]  # sorted, names only
    # No value leaks into the listing.
    assert not any("secret" in n for n in names)


def test_empty_and_unknown_project_is_safe(store):
    assert store.get_all("p_unknown") == {}
    assert store.get("p_unknown", "ANY") is None
    assert store.names("p_unknown") == []


# ---------------------------------------------------------------------------
# On-disk hygiene
# ---------------------------------------------------------------------------


def test_file_is_0600_and_dir_0700(store):
    store.set("p_alpha1", "K", "v")
    path = store.path_for("p_alpha1")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_corrupt_file_fails_closed(store):
    path = store.path_for("p_alpha1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    # Corrupt store -> empty (no secrets), never a crash on the spawn path.
    assert store.get_all("p_alpha1") == {}


def test_stored_json_holds_only_validated_pairs(store):
    store.set("p_alpha1", "K1", "v1")
    store.set("p_alpha1", "K2", "v2")
    data = json.loads(store.path_for("p_alpha1").read_text(encoding="utf-8"))
    assert data == {"K1": "v1", "K2": "v2"}


def test_traversal_project_id_rejected(store):
    with pytest.raises(psec.ProjectSecretError):
        store.path_for("../escape")
    with pytest.raises(psec.ProjectSecretError):
        store.set("../escape", "K", "v")


# ---------------------------------------------------------------------------
# Validation + redaction (never leak a value)
# ---------------------------------------------------------------------------


def test_invalid_name_rejected(store):
    for bad in ("1BAD", "has-dash", "has space", ""):
        with pytest.raises(psec.InvalidSecretName):
            store.set("p_alpha1", bad, "v")


def test_global_names_rejected(store):
    # A project secret must not be able to shadow a runtime pin.
    for reserved in ("HERMES_KANBAN_DB", "PATH", "HERMES_HOME"):
        with pytest.raises(psec.InvalidSecretName):
            store.set("p_alpha1", reserved, "v")


def test_non_string_and_null_value_rejected(store):
    with pytest.raises(psec.ProjectSecretError):
        store.set("p_alpha1", "K", 123)  # type: ignore[arg-type]
    with pytest.raises(psec.ProjectSecretError):
        store.set("p_alpha1", "K", "has\x00null")


def test_validation_error_never_contains_the_value():
    secret = "super-secret-value-1234"
    with pytest.raises(psec.ProjectSecretError) as exc:
        psec.validate_secret_value("K", 123)  # type: ignore[arg-type]
    assert secret not in str(exc.value)
    with pytest.raises(psec.ProjectSecretError) as exc2:
        psec.validate_secrets({"K": secret + "\x00"})
    assert secret not in str(exc2.value)


def test_redact_hides_value():
    assert psec.redact("abcd") == "<redacted:4 chars>"
    assert psec.redact("x") == "<redacted:1 char>"
    assert psec.redact(None) == "<unset>"
    assert "secret" not in psec.redact("secret")


# ---------------------------------------------------------------------------
# Resolution entry points
# ---------------------------------------------------------------------------


def test_build_scope_and_resolve(store):
    store.set("p_alpha1", "K", "v")
    assert psec.build_project_secret_scope("p_alpha1", store=store) == {"K": "v"}
    assert psec.build_project_secret_scope(None, store=store) == {}
    assert psec.build_project_secret_scope("p_none", store=store) == {}
    assert psec.resolve_project_secret("p_alpha1", "K", store=store) == "v"
    assert psec.resolve_project_secret("p_alpha1", "MISSING", store=store) is None
    assert psec.resolve_project_secret(None, "K", "d", store=store) == "d"


def test_build_scope_returns_fresh_dict(store):
    store.set("p_alpha1", "K", "v")
    scope = psec.build_project_secret_scope("p_alpha1", store=store)
    scope["K"] = "mutated"
    # Mutating the returned scope must not corrupt the store.
    assert store.get("p_alpha1", "K") == "v"


# ---------------------------------------------------------------------------
# macOS Keychain backend
# ---------------------------------------------------------------------------


class _FakeSecurity:
    """In-memory stand-in for the ``security`` CLI (add/find/delete-generic).

    Keyed by (service, account) -> password, so the store's command
    construction and JSON-blob round-trip are exercised without touching the
    real login Keychain. Return codes mirror ``security``: 0 on success, 44
    ("item not found") on a miss.
    """

    def __init__(self):
        self.items: dict = {}

    def run(self, cmd, **kwargs):
        assert cmd[0] == "security"
        sub = cmd[1]
        flags = self._parse(cmd[2:])
        key = (flags.get("-s"), flags.get("-a"))
        if sub == "add-generic-password":
            self.items[key] = flags["-w"]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if sub == "find-generic-password":
            if key not in self.items:
                return subprocess.CompletedProcess(cmd, 44, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, self.items[key] + "\n", "")
        if sub == "delete-generic-password":
            if key not in self.items:
                return subprocess.CompletedProcess(cmd, 44, "", "not found")
            del self.items[key]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected security subcommand {sub}")

    @staticmethod
    def _parse(argv) -> dict:
        out: dict = {}
        i = 0
        while i < len(argv):
            tok = argv[i]
            # ``-w`` takes a value on write (add) but is the value-less read flag
            # on find (where it is the last token).
            if tok in ("-s", "-a", "-D") or (tok == "-w" and i + 1 < len(argv)):
                out[tok] = argv[i + 1]
                i += 2
            else:  # value-less flags (-U, -A, read -w) or trailing keychain arg
                out.setdefault(tok, True)
                i += 1
        return out


@pytest.fixture
def fake_keychain(monkeypatch):
    fake = _FakeSecurity()
    monkeypatch.setattr("hermes_cli.project_secrets.subprocess.run", fake.run)
    monkeypatch.setattr(
        psec.KeychainProjectSecretStore, "is_available", staticmethod(lambda: True)
    )
    return fake


@pytest.fixture
def kc_store(fake_keychain):
    return psec.KeychainProjectSecretStore()


class TestKeychainStoreFaked:
    """Backend logic + command construction against a faked ``security``."""

    def test_set_get_roundtrip(self, kc_store):
        kc_store.set("p_alpha1", "ANTHROPIC_API_KEY", "sk-alpha")
        assert kc_store.get("p_alpha1", "ANTHROPIC_API_KEY") == "sk-alpha"
        assert kc_store.get_all("p_alpha1") == {"ANTHROPIC_API_KEY": "sk-alpha"}

    def test_two_projects_isolated(self, kc_store):
        kc_store.set("p_alpha1", "PROVIDER_KEY", "value-A")
        kc_store.set("p_beta22", "PROVIDER_KEY", "value-B")
        assert kc_store.get_all("p_alpha1") == {"PROVIDER_KEY": "value-A"}
        assert kc_store.get_all("p_beta22") == {"PROVIDER_KEY": "value-B"}
        assert kc_store.get("p_alpha1", "PROVIDER_KEY") == "value-A"

    def test_empty_and_unknown_project_is_safe(self, kc_store):
        assert kc_store.get_all("p_unknown") == {}
        assert kc_store.get("p_unknown", "ANY") is None
        assert kc_store.names("p_unknown") == []

    def test_delete(self, kc_store):
        kc_store.set("p_alpha1", "TOKEN_X", "v")
        assert kc_store.delete("p_alpha1", "TOKEN_X") is True
        assert kc_store.delete("p_alpha1", "TOKEN_X") is False
        assert kc_store.get("p_alpha1", "TOKEN_X") is None

    def test_deleting_last_secret_removes_item(self, kc_store, fake_keychain):
        kc_store.set("p_alpha1", "ONLY", "v")
        kc_store.delete("p_alpha1", "ONLY")
        # Empty set -> no husk item left in the keychain.
        assert (psec._KEYCHAIN_SERVICE, "p_alpha1") not in fake_keychain.items

    def test_stored_blob_is_json_map_only(self, kc_store, fake_keychain):
        kc_store.set("p_alpha1", "K1", "v1")
        kc_store.set("p_alpha1", "K2", "v2")
        raw = fake_keychain.items[(psec._KEYCHAIN_SERVICE, "p_alpha1")]
        assert json.loads(raw) == {"K1": "v1", "K2": "v2"}

    def test_corrupt_item_fails_closed(self, kc_store, fake_keychain):
        fake_keychain.items[(psec._KEYCHAIN_SERVICE, "p_alpha1")] = "{not json"
        assert kc_store.get_all("p_alpha1") == {}

    def test_write_deletes_then_adds_accessible(self, kc_store, monkeypatch):
        calls = []

        def _capture(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("hermes_cli.project_secrets.subprocess.run", _capture)
        kc_store.replace_all("p_alpha1", {"K": "v"})
        # delete-then-add: never ``add -U`` (that update path hangs on a prompt).
        assert calls[0][1] == "delete-generic-password"
        add = calls[1]
        assert add[1] == "add-generic-password"
        assert "-U" not in add
        assert "-A" in add  # non-interactive read for autonomous spawns
        assert add[add.index("-s") + 1] == psec._KEYCHAIN_SERVICE
        assert add[add.index("-a") + 1] == "p_alpha1"

    def test_read_never_passes_value_on_argv(self, kc_store, monkeypatch):
        seen = {}

        def _capture(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 44, "", "")

        monkeypatch.setattr("hermes_cli.project_secrets.subprocess.run", _capture)
        kc_store.get_all("p_alpha1")
        assert "-w" in seen["cmd"] and seen["cmd"][-1] == "-w"  # read flag, no value

    def test_traversal_project_id_rejected(self, kc_store):
        with pytest.raises(psec.ProjectSecretError):
            kc_store.set("../escape", "K", "v")
        with pytest.raises(psec.ProjectSecretError):
            kc_store.get_all("../escape")

    def test_invalid_name_rejected(self, kc_store):
        with pytest.raises(psec.InvalidSecretName):
            kc_store.set("p_alpha1", "has-dash", "v")

    def test_security_unavailable_fails_closed_on_read(self, kc_store, monkeypatch):
        def _boom(cmd, **kwargs):
            raise FileNotFoundError("security missing")

        monkeypatch.setattr("hermes_cli.project_secrets.subprocess.run", _boom)
        # Read must never crash the spawn path when the keychain is unreachable.
        assert kc_store.get_all("p_alpha1") == {}

    def test_security_failure_raises_on_write(self, kc_store, monkeypatch):
        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")

        monkeypatch.setattr("hermes_cli.project_secrets.subprocess.run", _fail)
        # A failed write must surface, never silently drop the secret.
        with pytest.raises(psec.ProjectSecretError):
            kc_store.replace_all("p_alpha1", {"K": "v"})


class TestDefaultBackendSelection:
    def test_macos_selects_keychain(self, monkeypatch):
        monkeypatch.setattr(
            psec.KeychainProjectSecretStore, "is_available", staticmethod(lambda: True)
        )
        assert isinstance(psec._build_default_store(), psec.KeychainProjectSecretStore)

    def test_non_macos_falls_back_to_file(self, monkeypatch):
        monkeypatch.setattr(
            psec.KeychainProjectSecretStore, "is_available", staticmethod(lambda: False)
        )
        assert isinstance(psec._build_default_store(), psec.FileProjectSecretStore)


@pytest.mark.skipif(
    not psec.KeychainProjectSecretStore.is_available(),
    reason="macOS Keychain (security) not available",
)
class TestKeychainStoreReal:
    """End-to-end against the real ``security`` in an isolated temp keychain.

    Never touches the user's login keychain: a throwaway keychain file is
    created/unlocked for the test and deleted in teardown.
    """

    @pytest.fixture
    def real_store(self, tmp_path):
        kc_path = str(tmp_path / f"hermes-test-{uuid.uuid4().hex[:8]}.keychain-db")
        subprocess.run(
            ["security", "create-keychain", "-p", "test", kc_path],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["security", "unlock-keychain", "-p", "test", kc_path],
            check=True, capture_output=True, text=True,
        )
        service = f"hermes-project-secrets-test-{uuid.uuid4().hex[:8]}"
        try:
            yield psec.KeychainProjectSecretStore(service=service, keychain=kc_path)
        finally:
            subprocess.run(
                ["security", "delete-keychain", kc_path],
                capture_output=True, text=True,
            )

    def test_real_roundtrip_and_isolation(self, real_store):
        real_store.set("p_alpha1", "PROVIDER_KEY", "value-A")
        real_store.set("p_beta22", "PROVIDER_KEY", "value-B")
        real_store.set("p_alpha1", "ALPHA_ONLY", "only-A")

        assert real_store.get("p_alpha1", "PROVIDER_KEY") == "value-A"
        assert real_store.get("p_beta22", "PROVIDER_KEY") == "value-B"
        assert real_store.get_all("p_alpha1") == {
            "PROVIDER_KEY": "value-A", "ALPHA_ONLY": "only-A",
        }
        assert "only-A" not in real_store.get_all("p_beta22").values()

    def test_real_delete_and_empty(self, real_store):
        real_store.set("p_alpha1", "K", "v")
        assert real_store.delete("p_alpha1", "K") is True
        assert real_store.get_all("p_alpha1") == {}
        assert real_store.delete("p_alpha1", "K") is False


# ---------------------------------------------------------------------------
# Spawn-level isolation (the acceptance criterion) — env-inject at spawn
# ---------------------------------------------------------------------------


def _make_task(*, task_id, project_id, workspace):
    return kb.Task(
        id=task_id,
        title="x",
        body=None,
        assignee="coder",
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=workspace,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        branch_name=f"wt/{task_id}",
        project_id=project_id,
        executor="hermes-worker",
    )


class TestSpawnSecretIsolation:
    """`_default_spawn` must hand each project's worker ONLY its own secrets."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    def _capture_spawn_env(self, monkeypatch, task, workspace):
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = dict(kwargs.get("env", {}))
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        kb._default_spawn(task, workspace)
        return captured["env"]

    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        self._set_home(monkeypatch, tmp_path, home)
        store = psec.FileProjectSecretStore(home=home)
        psec.set_default_store(store)
        try:
            yield store
        finally:
            psec.set_default_store(None)

    def test_worker_env_carries_only_its_own_project_secret(
        self, tmp_path, monkeypatch, _isolated_store
    ):
        _isolated_store.set("p_alpha1", "PROVIDER_KEY", "value-A")
        _isolated_store.set("p_beta22", "PROVIDER_KEY", "value-B")
        _isolated_store.set("p_alpha1", "ALPHA_ONLY", "only-A")

        ws = str(tmp_path / "ws")
        env_a = self._capture_spawn_env(
            monkeypatch, _make_task(task_id="t_a", project_id="p_alpha1", workspace=ws), ws
        )
        env_b = self._capture_spawn_env(
            monkeypatch, _make_task(task_id="t_b", project_id="p_beta22", workspace=ws), ws
        )

        # Correct resolution: each worker sees its own project's value.
        assert env_a["PROVIDER_KEY"] == "value-A"
        assert env_b["PROVIDER_KEY"] == "value-B"
        # No cross-project access: neither worker can see the other's value.
        assert env_a["PROVIDER_KEY"] != env_b["PROVIDER_KEY"]
        assert "value-B" not in env_a.values()
        assert "value-A" not in env_b.values()
        # A project-exclusive key is present for its owner, absent for the peer.
        assert env_a.get("ALPHA_ONLY") == "only-A"
        assert "ALPHA_ONLY" not in env_b

    def test_unlinked_task_injects_nothing(self, tmp_path, monkeypatch, _isolated_store):
        _isolated_store.set("p_alpha1", "PROVIDER_KEY", "value-A")
        ws = str(tmp_path / "ws")
        env = self._capture_spawn_env(
            monkeypatch, _make_task(task_id="t_x", project_id=None, workspace=ws), ws
        )
        # Backward compatible: no project link => no project secret injected.
        assert env.get("PROVIDER_KEY") is None

    def test_missing_project_store_is_safe(self, tmp_path, monkeypatch, _isolated_store):
        ws = str(tmp_path / "ws")
        # Project linked but no secrets stored -> spawn still succeeds, nothing added.
        env = self._capture_spawn_env(
            monkeypatch, _make_task(task_id="t_y", project_id="p_nostore", workspace=ws), ws
        )
        assert "PROVIDER_KEY" not in env
        # Control-plane pins are still present and correct.
        assert env["HERMES_KANBAN_TASK"] == "t_y"

    def test_project_secret_never_clobbers_control_plane_pin(
        self, tmp_path, monkeypatch, _isolated_store
    ):
        # A provider key is injected, but the kanban DB pin (set after injection)
        # remains authoritative - a project can never redirect the board.
        _isolated_store.set("p_alpha1", "PROVIDER_KEY", "value-A")
        ws = str(tmp_path / "ws")
        env = self._capture_spawn_env(
            monkeypatch, _make_task(task_id="t_z", project_id="p_alpha1", workspace=ws), ws
        )
        assert env["PROVIDER_KEY"] == "value-A"
        assert env["HERMES_KANBAN_DB"].endswith("kanban.db")
