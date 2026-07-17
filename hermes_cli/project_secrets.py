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

Storage: on macOS (the *standard of care*) secrets live encrypted in the OS
login Keychain via :class:`KeychainProjectSecretStore` - never a plaintext file,
so they are excluded from Time Machine and backups. Other platforms fall back to
:class:`FileProjectSecretStore`: a gitignored, ``0600`` JSON file at
``$HERMES_HOME/projects/<project_id>/secrets.json``, co-located with the
per-profile ``projects.db``. Both sit behind the :class:`ProjectSecretStore`
interface, so :func:`get_default_store` swaps backend by platform without
touching any caller (native Windows / Linux keychain backends deferred).

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
        _atomic_write_private(path, payload)


def _atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with ``0600`` permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets_", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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
# macOS Keychain backend
# ---------------------------------------------------------------------------

# One login-Keychain generic-password item per project; its password field
# holds that project's ``{NAME: value}`` map as a JSON string. This mirrors how
# Claude Code stores its OAuth payload (see
# :func:`agent.claude_subscriptions._read_credentials_keychain`).
_KEYCHAIN_SERVICE = "hermes-project-secrets"
_SECURITY_TIMEOUT = 5


class KeychainProjectSecretStore(ProjectSecretStore):
    """macOS Keychain backend - the *standard of care* for project secrets.

    Secret material is stored encrypted in the OS login Keychain instead of a
    plaintext file, so it is never exposed to Time Machine snapshots, cloud or
    local backups, or a stray ``cat`` of the profile directory - the concrete
    at-rest risk the file backend carries. The store keeps one generic-password
    item per ``project_id`` (service ``hermes-project-secrets``) whose password
    field is the project's validated ``{NAME: value}`` map as JSON, so
    :meth:`get_all` / :meth:`replace_all` stay a single Keychain read / write.

    macOS only; :meth:`is_available` gates selection so non-Darwin hosts keep
    the :class:`FileProjectSecretStore` fallback (a real OS-keychain backend for
    Windows / Linux is deferred behind this same interface).

    Known limitation: writes shell out to ``security add-generic-password -w``,
    which places the value on the process argv (briefly visible to a same-user
    ``ps``). Items are created ``-A`` (any same-user app may read) so autonomous
    spawns never block on an interactive Keychain-access prompt. Both are
    same-user exposures no worse than the ``0600`` file this replaces; the win
    is encryption at rest and exclusion from backups.
    """

    def __init__(self, *, service: Optional[str] = None, keychain: Optional[str] = None):
        # ``keychain`` targets a specific keychain file (tests point at an
        # isolated temp keychain); ``None`` uses the user's default search list.
        self._service = service or _KEYCHAIN_SERVICE
        self._keychain = keychain

    @staticmethod
    def is_available() -> bool:
        """Whether this host can use the Keychain backend (macOS + ``security``)."""
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

    def get_all(self, project_id: str) -> Dict[str, str]:
        account = _safe_project_id(project_id)
        result = self._run([
            "find-generic-password", "-s", self._service, "-a", account, "-w",
        ])
        if result is None:
            _log.warning("keychain unavailable reading project secrets for %s "
                         "(treating as empty)", project_id)
            return {}
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        try:
            data = json.loads(result.stdout.strip())
        except (ValueError, TypeError):
            # Corrupt item -> fail closed (no secrets), never crash the spawn
            # path. Log the project only, never any parsed content.
            _log.warning("project secrets keychain item for %s is unreadable; "
                         "treating as empty", project_id)
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
        account = _safe_project_id(project_id)
        cleaned = validate_secrets(secrets)
        # Always delete-then-add. ``add -U`` (update-in-place) of an existing
        # item blocks on an interactive Keychain-authorization prompt, which
        # would hang an autonomous spawn; deleting first (ignore "not found")
        # then adding fresh never prompts. Also leaves no empty husk when the
        # set becomes empty.
        self._run(["delete-generic-password", "-s", self._service, "-a", account])
        if not cleaned:
            return
        payload = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
        result = self._run([
            "add-generic-password", "-s", self._service, "-a", account,
            "-w", payload, "-A", "-D", "hermes project secrets",
        ])
        if result is None:
            raise ProjectSecretError(
                f"could not write project secrets for {project_id}: the macOS "
                "Keychain (security) is unavailable or timed out"
            )
        if result.returncode != 0:
            raise ProjectSecretError(
                f"could not write project secrets for {project_id} to the macOS "
                f"Keychain (security exited {result.returncode})"
            )


# ---------------------------------------------------------------------------
# Default store + resolution entry points
# ---------------------------------------------------------------------------

_default_store: Optional[ProjectSecretStore] = None


def _build_default_store() -> ProjectSecretStore:
    """Keychain on macOS (standard of care); file backend elsewhere."""
    if KeychainProjectSecretStore.is_available():
        return KeychainProjectSecretStore()
    return FileProjectSecretStore()


def get_default_store() -> ProjectSecretStore:
    """Return the process default store.

    macOS resolves to :class:`KeychainProjectSecretStore`; other platforms keep
    the :class:`FileProjectSecretStore` until a native backend lands for them.
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
