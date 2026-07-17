"""Project-scoped credential + provider-key isolation.

A first-class :class:`~hermes_cli.projects_db.Project` may own its own provider
API keys and agent credentials. A kanban task linked to a project (via
``tasks.project_id``) must run with **only that project's** secrets and must
never be able to reach another project's keys.

This module is the project analogue of :mod:`agent.secret_scope` (which does
the same isolation per *profile*). The invariant is identical and just as
strict:

- Project secrets live in a per-project file and are **NEVER** unioned into the
  process-global ``os.environ``. Unioning them would leak project A's keys to
  project B's worker (and to every subprocess spawned with
  ``env=dict(os.environ)``).
- Resolution is per ``project_id``. A missing secret returns the caller's
  default (a *safe* absence) - it never falls through to another project or to
  ``os.environ``.

Storage: on macOS (the *standard of care*) secrets are encrypted at rest. A
single random master key lives in the OS login Keychain
(:class:`KeychainMasterKey`) - one item, created once, read non-interactively -
and each project's secret map is stored as authenticated ciphertext in a
``0600`` file at ``$HERMES_HOME/projects/<project_id>/secrets.enc``
(:class:`EncryptedFileProjectSecretStore`), decrypted only in memory. This gives
encryption at rest (the file is useless in a Time Machine snapshot or backup
without the Keychain-held key) with **zero per-secret Keychain prompts** - the
naive "one Keychain item per secret/project" backend prompted on first access to
each item and was rejected. Other platforms fall back to the plaintext
:class:`FileProjectSecretStore` until a native secret backend lands for them
(Windows Credential Manager / Linux Secret Service, and product backends such as
1Password Service Accounts / Vault, are deferred behind this same
:class:`ProjectSecretStore` interface). :func:`get_default_store` swaps backend
by platform without touching any caller.

The resolution contract is **env-inject at spawn**: the kanban dispatcher's
``_default_spawn`` layers a task's project secrets over the child's inherited
environment (project keys shadow the global default for that project's worker
only). Both executor paths - the native ``hermes-worker`` and the external ACP
harnesses (``claude-code`` / ``codex``) - spawn through that one chokepoint, so
both honour the contract uniformly.

No secret *value* is ever written to a log, an error message, or an API
response by this module. Use :func:`redact` when a diagnostic needs to
reference a value at all.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from agent.secret_scope import _is_global_env
from agent.secret_sources.base import is_valid_env_name
from hermes_constants import get_hermes_home
from utils import atomic_replace

_log = logging.getLogger(__name__)

_SECRETS_FILENAME = "secrets.json"

# project_id is a store-path component - keep it strict so a crafted id can
# never escape the project's own directory (``../`` traversal, separators).
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# Errors + redaction
# ---------------------------------------------------------------------------


class ProjectSecretError(ValueError):
    """A project-secret validation failure. Messages never include a value."""


class InvalidSecretName(ProjectSecretError):
    """A secret name is not a legal / permitted env-var name."""


def redact(value: object) -> str:
    """Return a log-safe placeholder for a secret value - never the value.

    Reveals only the length so a diagnostic can distinguish "empty" from "set"
    without exposing the material itself.
    """
    if value is None:
        return "<unset>"
    n = len(str(value))
    return f"<redacted:{n} char{'' if n == 1 else 's'}>"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_secret_name(name: object) -> str:
    """Validate + return a project-secret env-var name.

    Rejects illegal env-var names and Hermes-global / deployment-level names
    (:func:`agent.secret_scope._is_global_env`) - a project secret must never
    shadow a runtime pin like ``HERMES_KANBAN_DB`` or ``PATH``.
    """
    s = str(name or "").strip()
    if not is_valid_env_name(s):
        raise InvalidSecretName(
            f"invalid secret name {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    if _is_global_env(s):
        raise InvalidSecretName(
            f"{s} is a Hermes-global / deployment variable and cannot be stored "
            "as a project secret (it would shadow a runtime setting)"
        )
    return s


def validate_secret_value(name: str, value: object) -> str:
    """Validate one secret value. Error text references the name, never the value."""
    if not isinstance(value, str):
        raise ProjectSecretError(
            f"secret {name}: value must be a string, got {type(value).__name__}"
        )
    if "\x00" in value:
        raise ProjectSecretError(f"secret {name}: value contains a null byte")
    return value


def validate_secrets(mapping: object) -> Dict[str, str]:
    """Validate a ``{NAME: value}`` mapping; return a cleaned copy.

    Raises :class:`ProjectSecretError` on the first bad entry, naming the
    offending key only.
    """
    if not isinstance(mapping, dict):
        raise ProjectSecretError("secrets must be a mapping of {NAME: value}")
    cleaned: Dict[str, str] = {}
    for name, value in mapping.items():
        cname = validate_secret_name(name)
        cleaned[cname] = validate_secret_value(cname, value)
    return cleaned


def _safe_project_id(project_id: object) -> str:
    pid = str(project_id or "").strip()
    if not _PROJECT_ID_RE.match(pid):
        raise ProjectSecretError(f"invalid project id {project_id!r}")
    return pid


# ---------------------------------------------------------------------------
# Store interface + filesystem backend
# ---------------------------------------------------------------------------


class ProjectSecretStore(ABC):
    """Backend for per-project secrets.

    Subclasses implement the two storage primitives (:meth:`get_all` /
    :meth:`replace_all`); the read/write conveniences are shared so every
    backend enforces the same validation and never-leak rules.
    """

    @abstractmethod
    def get_all(self, project_id: str) -> Dict[str, str]:
        """Return this project's full ``{NAME: value}`` map (``{}`` when none).

        Must never raise for a missing/empty store and must never return
        another project's secrets.
        """

    @abstractmethod
    def replace_all(self, project_id: str, secrets: Dict[str, str]) -> None:
        """Atomically replace this project's secret set (validated by caller)."""

    def get(self, project_id: str, name: str, default: Optional[str] = None):
        """Resolve one secret. Absent -> ``default`` (never another project)."""
        return self.get_all(project_id).get(str(name), default)

    def names(self, project_id: str) -> List[str]:
        """Sorted secret NAMES for a project (names only - never values)."""
        return sorted(self.get_all(project_id))

    def set(self, project_id: str, name: str, value: str) -> str:
        """Set/overwrite one secret. Returns the canonical name."""
        cname = validate_secret_name(name)
        cvalue = validate_secret_value(cname, value)
        current = self.get_all(project_id)
        current[cname] = cvalue
        self.replace_all(project_id, current)
        return cname

    def delete(self, project_id: str, name: str) -> bool:
        """Delete one secret. Returns True when it existed."""
        current = self.get_all(project_id)
        if str(name) not in current:
            return False
        del current[str(name)]
        self.replace_all(project_id, current)
        return True


class FileProjectSecretStore(ProjectSecretStore):
    """Filesystem backend: one ``0600`` JSON file per project under HERMES_HOME.

    ``home`` pins the profile root (tests pass an explicit path); when ``None``
    it resolves lazily via :func:`get_hermes_home` on every access, so a profile
    switch is picked up without rebuilding the store.
    """

    def __init__(self, home: Optional[Path] = None):
        self._home = Path(home) if home is not None else None

    def _home_dir(self) -> Path:
        return self._home if self._home is not None else get_hermes_home()

    def path_for(self, project_id: str) -> Path:
        pid = _safe_project_id(project_id)
        return self._home_dir() / "projects" / pid / _SECRETS_FILENAME

    def get_all(self, project_id: str) -> Dict[str, str]:
        path = self.path_for(project_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Corrupt store -> fail closed (no secrets) rather than crash the
            # spawn path. Log the path only, never any parsed content.
            _log.warning("project secrets file for %s is unreadable; "
                         "treating as empty (%s)", project_id, path)
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, str] = {}
        for name, value in data.items():
            if isinstance(name, str) and isinstance(value, str) \
                    and is_valid_env_name(name):
                out[name] = value
        return out

    def replace_all(self, project_id: str, secrets: Dict[str, str]) -> None:
        cleaned = validate_secrets(secrets)
        path = self.path_for(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True)
        _atomic_write_private(path, payload.encode("utf-8"))


def _atomic_write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with ``0600`` permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets_", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# macOS master key (Keychain) + encrypted-file backend
# ---------------------------------------------------------------------------

# ONE login-Keychain generic-password item holds a single random data-encryption
# key for all project secrets. Per-project secret maps are AEAD-encrypted with
# it and stored on disk (see :class:`EncryptedFileProjectSecretStore`); the
# Keychain never holds a per-project or per-secret item, so there is no
# per-secret access prompt. Mirrors how Claude Code parks material in the
# Keychain (see :func:`agent.claude_subscriptions._read_credentials_keychain`).
_MASTER_KEY_SERVICE = "hermes-project-secrets-master"
_MASTER_KEY_ACCOUNT = "master"
_SECURITY_TIMEOUT = 5


class MasterKeyUnavailable(ProjectSecretError):
    """The at-rest master key could not be read or created (no secure backend)."""


class KeychainMasterKey:
    """A single Fernet master key parked in the macOS login Keychain.

    :meth:`get_or_create` returns the process-wide data key, creating it on
    first use. Exactly one Keychain item ever exists (service
    ``hermes-project-secrets-master``); it is created ``-A`` (readable by any
    same-user app without a prompt) so an autonomous spawn never blocks on an
    interactive Keychain-access dialog, and the value is cached in memory so the
    Keychain is touched at most once per process. The key is **never** deleted
    or rotated here - doing so would orphan every existing ciphertext.

    Known limitation: creation shells out to ``security add-generic-password
    -w``, briefly placing the key on the process argv (visible to a same-user
    ``ps``). This happens once, ever; reads pass no value on argv.
    """

    def __init__(self, *, service: Optional[str] = None, keychain: Optional[str] = None):
        # ``keychain`` targets a specific keychain file (tests point at an
        # isolated temp keychain); ``None`` uses the user's default search list.
        self._service = service or _MASTER_KEY_SERVICE
        self._keychain = keychain
        self._cached: Optional[bytes] = None

    @staticmethod
    def is_available() -> bool:
        """Whether this host can use the Keychain (macOS + ``security``)."""
        return platform.system() == "Darwin" and shutil.which("security") is not None

    def _run(self, args: List[str]) -> Optional[subprocess.CompletedProcess]:
        cmd = ["security", *args]
        if self._keychain is not None:
            cmd.append(self._keychain)
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_SECURITY_TIMEOUT, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _read(self) -> Optional[bytes]:
        """Return the stored key, or ``None`` if no item exists.

        Raises :class:`MasterKeyUnavailable` when the Keychain is unreachable or
        the stored value is not a valid key (never silently mints a new key over
        a corrupt one - that would orphan existing ciphertext).
        """
        result = self._run([
            "find-generic-password", "-s", self._service,
            "-a", _MASTER_KEY_ACCOUNT, "-w",
        ])
        if result is None:
            raise MasterKeyUnavailable("macOS Keychain (security) is unavailable")
        if result.returncode != 0 or not result.stdout.strip():
            return None
        key = result.stdout.strip().encode("ascii")
        try:
            Fernet(key)  # validates length + base64 shape
        except (ValueError, TypeError) as exc:
            raise MasterKeyUnavailable(
                "the stored project-secrets master key is corrupt; refusing to "
                "mint a new one (it would orphan existing secrets)"
            ) from exc
        return key

    def get_or_create(self) -> bytes:
        if self._cached is not None:
            return self._cached
        key = self._read()
        if key is None:
            key = Fernet.generate_key()
            added = self._run([
                "add-generic-password", "-s", self._service,
                "-a", _MASTER_KEY_ACCOUNT, "-w", key.decode("ascii"),
                "-A", "-D", "hermes project secrets master key",
            ])
            if added is None:
                raise MasterKeyUnavailable(
                    "macOS Keychain (security) is unavailable or timed out"
                )
            if added.returncode != 0:
                # A concurrent process may have won the create race; re-read and
                # adopt the winner rather than failing.
                key = self._read()
                if key is None:
                    raise MasterKeyUnavailable(
                        f"could not persist the master key (security exited "
                        f"{added.returncode})"
                    )
        self._cached = key
        return key


class EncryptedFileProjectSecretStore(ProjectSecretStore):
    """Encrypted-at-rest backend - the *standard of care* for project secrets.

    Each project's validated ``{NAME: value}`` map is serialised to JSON and
    stored as authenticated (Fernet / AES-CBC + HMAC) ciphertext in a ``0600``
    file at ``$HERMES_HOME/projects/<project_id>/secrets.enc``. The data key
    comes from a :class:`KeychainMasterKey`, so the on-disk file is useless in a
    Time Machine snapshot or backup without the Keychain-held key, and plaintext
    exists only in process memory - the concrete at-rest risk the plaintext file
    backend carries, closed without any per-secret Keychain prompt.

    ``home`` pins the profile root (tests pass an explicit path); when ``None``
    it resolves lazily via :func:`get_hermes_home` on every access.
    """

    def __init__(self, *, home: Optional[Path] = None, master: KeychainMasterKey):
        self._home = Path(home) if home is not None else None
        self._master = master

    def _home_dir(self) -> Path:
        return self._home if self._home is not None else get_hermes_home()

    def path_for(self, project_id: str) -> Path:
        pid = _safe_project_id(project_id)
        return self._home_dir() / "projects" / pid / "secrets.enc"

    def get_all(self, project_id: str) -> Dict[str, str]:
        path = self.path_for(project_id)
        try:
            token = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, OSError):
            return {}
        if not token.strip():
            return {}
        try:
            key = self._master.get_or_create()
        except MasterKeyUnavailable:
            _log.warning("master key unavailable reading project secrets for %s "
                         "(treating as empty)", project_id)
            return {}
        try:
            raw = Fernet(key).decrypt(token)
            data = json.loads(raw)
        except (InvalidToken, ValueError, TypeError):
            # Corrupt / undecryptable store -> fail closed (no secrets), never
            # crash the spawn path. Log the project only, never any content.
            _log.warning("project secrets file for %s is unreadable; "
                         "treating as empty (%s)", project_id, path)
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, str] = {}
        for name, value in data.items():
            if isinstance(name, str) and isinstance(value, str) \
                    and is_valid_env_name(name):
                out[name] = value
        return out

    def replace_all(self, project_id: str, secrets: Dict[str, str]) -> None:
        cleaned = validate_secrets(secrets)
        path = self.path_for(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        if not cleaned:
            # Empty set -> remove the file rather than leave an empty husk.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        key = self._master.get_or_create()  # raises MasterKeyUnavailable on failure
        payload = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
        token = Fernet(key).encrypt(payload.encode("utf-8"))
        _atomic_write_private(path, token)


# ---------------------------------------------------------------------------
# Default store + resolution entry points
# ---------------------------------------------------------------------------

_default_store: Optional[ProjectSecretStore] = None


def _build_default_store() -> ProjectSecretStore:
    """Encrypted store keyed off the Keychain on macOS; file backend elsewhere."""
    if KeychainMasterKey.is_available():
        return EncryptedFileProjectSecretStore(master=KeychainMasterKey())
    return FileProjectSecretStore()


def get_default_store() -> ProjectSecretStore:
    """Return the process default store.

    macOS resolves to :class:`EncryptedFileProjectSecretStore` (a Keychain-held
    master key encrypting per-project files); other platforms keep the plaintext
    :class:`FileProjectSecretStore` until a native backend lands for them.
    """
    global _default_store
    if _default_store is None:
        _default_store = _build_default_store()
    return _default_store


def set_default_store(store: Optional[ProjectSecretStore]) -> None:
    """Override the default store (tests, or a future keychain backend)."""
    global _default_store
    _default_store = store


def build_project_secret_scope(
    project_id: Optional[str], *, store: Optional[ProjectSecretStore] = None
) -> Dict[str, str]:
    """Return a project's secret mapping as a fresh dict (``{}`` when none).

    The project analogue of :func:`agent.secret_scope.build_profile_secret_scope`.
    Safe by construction: an unknown/empty/absent project yields ``{}`` - never
    another project's secrets, never ``os.environ``.
    """
    if not project_id:
        return {}
    st = store or get_default_store()
    return dict(st.get_all(str(project_id)))


def resolve_project_secret(
    project_id: Optional[str],
    name: str,
    default: Optional[str] = None,
    *,
    store: Optional[ProjectSecretStore] = None,
) -> Optional[str]:
    """Resolve ONE secret for a project. Absent -> ``default``.

    Never falls through to another project or to ``os.environ`` - a missing
    secret is a safe, explicit absence.
    """
    if not project_id:
        return default
    st = store or get_default_store()
    return st.get(str(project_id), name, default)
