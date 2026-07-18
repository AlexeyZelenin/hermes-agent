"""Sealed-core / userland layer model, self-modify toggle, guarded local
updates, and two-layer backup + restore.

Hermes state splits into two independently-addressable layers:

  * **sealed-core** — the immutable install tree (the ``hermes-agent`` code
    checkout; ``/opt/hermes`` under Docker). It carries the agent's own code
    and is meant to change only through a controlled process, and only while
    the ``self-modify`` toggle is ON.
  * **userland** — ``HERMES_HOME`` (``~/.hermes``): config, ``.env``, memory,
    skills, project DBs. Freely mutable and never touched by a sealed-core
    update.

The **self-modify toggle** (config ``self_modify.enabled`` / env
``HERMES_SELF_MODIFY``) governs whether the running system may rewrite its own
sealed-core. When OFF (the safe default), any sealed-core mutation via
:func:`apply_update` is refused with :class:`SelfModifyBlocked`. Userland
mutations are always allowed.

Backups snapshot either layer (or both) into
``HERMES_HOME/backups/layers/<id>.zip`` with a JSON manifest;
:func:`restore_layer` returns a layer to a captured snapshot — the rollback
path after a failed update. :func:`apply_update` auto-backs-up the target
layer before applying a staged local update and surgically rolls back on
failure.

Only the *local apply* of an already-staged update lives here; fetching the
update over the network (git/CDN) is out of scope and handled by
``hermes update``.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Union

from hermes_constants import get_hermes_home

# Reuse the canonical exclusion rules and SQLite safe-copy from the existing
# backup machinery rather than re-deriving a second "regeneratable dir" set.
from hermes_cli.backup import (  # noqa: E402
    _EXCLUDED_DIRS,
    _EXCLUDED_SUFFIXES,
    _safe_copy_db,
)

logger = logging.getLogger(__name__)

_SEALED_CORE_ROOT_ENV = "HERMES_SEALED_CORE_ROOT"
_SELF_MODIFY_ENV = "HERMES_SELF_MODIFY"
_SELF_MODIFY_CONFIG_KEY = "self_modify"
_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}


class Layer(str, enum.Enum):
    """The two independently-addressable state layers."""

    SEALED_CORE = "sealed-core"
    USERLAND = "userland"


class SelfModifyBlocked(RuntimeError):
    """Raised when a sealed-core mutation is attempted while self-modify is off."""


class LayerBackupNotFound(RuntimeError):
    """Raised when a referenced backup id has no manifest on disk."""


@dataclass(frozen=True)
class LayerBackup:
    """A captured snapshot of one layer."""

    backup_id: str
    layer: Layer
    path: Path
    manifest: dict


@dataclass(frozen=True)
class RestoreResult:
    layer: Layer
    root: Path
    restored: int
    removed: int


@dataclass(frozen=True)
class UpdateResult:
    layer: Layer
    root: Path
    changed: int
    backup: Optional[LayerBackup]


# ---------------------------------------------------------------------------
# Layer addressing
# ---------------------------------------------------------------------------

def sealed_core_root() -> Path:
    """Filesystem root of the immutable install tree (the code layer).

    Honours ``HERMES_SEALED_CORE_ROOT`` so Docker (``/opt/hermes``) and tests
    can point the layer elsewhere; otherwise the ``hermes-agent`` checkout that
    this module ships inside.
    """
    override = os.environ.get(_SEALED_CORE_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    from hermes_cli.config import get_project_root

    return get_project_root()


def userland_root() -> Path:
    """Filesystem root of the freely-mutable userland layer (HERMES_HOME)."""
    return get_hermes_home()


def layer_root(layer: Layer) -> Path:
    return sealed_core_root() if layer is Layer.SEALED_CORE else userland_root()


# ---------------------------------------------------------------------------
# self-modify toggle
# ---------------------------------------------------------------------------

def _env_toggle() -> Optional[bool]:
    raw = os.environ.get(_SELF_MODIFY_ENV, "").strip().lower()
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    return None


def is_self_modify_enabled() -> bool:
    """Whether the system is currently allowed to modify its own sealed-core.

    Precedence: ``HERMES_SELF_MODIFY`` env override, then config
    ``self_modify.enabled``, then the safe default of ``False`` (locked).
    """
    env = _env_toggle()
    if env is not None:
        return env
    try:
        from hermes_cli.config import load_config_readonly

        section = (load_config_readonly() or {}).get(_SELF_MODIFY_CONFIG_KEY, {})
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
    except Exception as exc:  # never let a config read failure unlock the core
        logger.debug("self-modify config read failed: %s", exc)
    return False


def set_self_modify(enabled: bool) -> None:
    """Persist the self-modify toggle to config.yaml (userland)."""
    from hermes_cli.config import load_config, save_config

    cfg = load_config() or {}
    section = cfg.get(_SELF_MODIFY_CONFIG_KEY)
    if not isinstance(section, dict):
        section = {}
    section["enabled"] = bool(enabled)
    cfg[_SELF_MODIFY_CONFIG_KEY] = section
    save_config(cfg)


# ---------------------------------------------------------------------------
# Layer file walk (shared by backup + rollback)
# ---------------------------------------------------------------------------

def _iter_layer_files(root: Path) -> Iterator[tuple[Path, Path]]:
    """Yield ``(abs_path, rel_path)`` for every non-excluded file under root."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            if fname.endswith(_EXCLUDED_SUFFIXES):
                continue
            fpath = dp / fname
            try:
                rel = fpath.relative_to(root)
            except ValueError:
                continue
            yield fpath, rel


def _add_to_zip(zf: zipfile.ZipFile, abs_path: Path, rel: Path, staging: Path) -> bool:
    """Add one file, taking a consistent snapshot for live ``*.db`` files."""
    try:
        if abs_path.suffix == ".db":
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=str(staging)) as tmp:
                tmp_db = Path(tmp.name)
            try:
                if not _safe_copy_db(abs_path, tmp_db):
                    return False
                zf.write(tmp_db, arcname=str(rel))
            finally:
                tmp_db.unlink(missing_ok=True)
        else:
            zf.write(abs_path, arcname=str(rel))
    except (PermissionError, OSError, ValueError) as exc:
        logger.debug("skip %s in layer zip: %s", rel, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backups_root(home: Optional[Path] = None) -> Path:
    return (home or userland_root()) / "backups" / "layers"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_layer(
    layer: Layer,
    *,
    home: Optional[Path] = None,
    timestamp: Optional[str] = None,
) -> Optional[LayerBackup]:
    """Snapshot one layer into ``backups/layers/<layer>-<ts>.zip`` + manifest.

    Returns the :class:`LayerBackup`, or ``None`` when the layer root is
    missing or held no backable files.
    """
    root = layer_root(layer)
    if not root.is_dir():
        return None
    stamp = timestamp or _now_stamp()
    out_dir = backups_root(home)
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_id = f"{layer.value}-{stamp}"
    zip_path = out_dir / f"{backup_id}.zip"

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for abs_path, rel in _iter_layer_files(root):
            if _add_to_zip(zf, abs_path, rel, out_dir):
                count += 1
    if count == 0:
        zip_path.unlink(missing_ok=True)
        return None

    manifest = {
        "backup_id": backup_id,
        "layer": layer.value,
        "source_root": str(root),
        "file_count": count,
        "created_at": stamp,
    }
    (out_dir / f"{backup_id}.manifest.json").write_text(json.dumps(manifest, indent=2))
    return LayerBackup(backup_id, layer, zip_path, manifest)


def backup_all(
    *, home: Optional[Path] = None, timestamp: Optional[str] = None
) -> List[LayerBackup]:
    """Back up both layers under a shared timestamp; skips empty/missing layers."""
    stamp = timestamp or _now_stamp()
    out: List[LayerBackup] = []
    for layer in (Layer.SEALED_CORE, Layer.USERLAND):
        ref = backup_layer(layer, home=home, timestamp=stamp)
        if ref is not None:
            out.append(ref)
    return out


def list_backups(home: Optional[Path] = None) -> List[LayerBackup]:
    """All layer backups on disk, newest id first."""
    out_dir = backups_root(home)
    if not out_dir.is_dir():
        return []
    out: List[LayerBackup] = []
    for manifest_path in sorted(out_dir.glob("*.manifest.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text())
            zip_path = out_dir / f"{manifest['backup_id']}.zip"
            if zip_path.exists():
                out.append(
                    LayerBackup(
                        manifest["backup_id"], Layer(manifest["layer"]), zip_path, manifest
                    )
                )
        except (OSError, ValueError, KeyError) as exc:
            logger.debug("skip malformed manifest %s: %s", manifest_path.name, exc)
    return out


def _resolve_backup(
    backup: Union[LayerBackup, str], home: Optional[Path] = None
) -> LayerBackup:
    if isinstance(backup, LayerBackup):
        return backup
    for ref in list_backups(home):
        if ref.backup_id == backup:
            return ref
    raise LayerBackupNotFound(f"no layer backup with id {backup!r}")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _safe_members(zf: zipfile.ZipFile, root: Path) -> List[str]:
    """Reject path-traversal / absolute entries before extracting."""
    safe: List[str] = []
    for name in zf.namelist():
        dest = (root / name).resolve()
        if dest == root.resolve() or root.resolve() in dest.parents:
            safe.append(name)
        else:
            logger.warning("refusing unsafe zip member %r", name)
    return safe


def _remove_untracked(root: Path, kept: set[str]) -> int:
    removed = 0
    for _, rel in list(_iter_layer_files(root)):
        if str(rel) not in kept:
            try:
                (root / rel).unlink()
                removed += 1
            except OSError as exc:
                logger.debug("could not remove untracked %s: %s", rel, exc)
    return removed


def restore_layer(
    backup: Union[LayerBackup, str],
    *,
    home: Optional[Path] = None,
    target_root: Optional[Path] = None,
    clean: bool = False,
) -> RestoreResult:
    """Restore a layer from a backup by overlaying its files onto the root.

    With ``clean=True`` this becomes a true snapshot restore: non-excluded
    files under the root that are *not* in the backup are removed, so the tree
    matches the captured state exactly (used to undo files a bad update added).
    """
    ref = _resolve_backup(backup, home)
    root = target_root or layer_root(ref.layer)
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ref.path) as zf:
        members = _safe_members(zf, root)
        zf.extractall(root, members=members)
    removed = _remove_untracked(root, set(members)) if clean else 0
    return RestoreResult(ref.layer, root, len(members), removed)


# ---------------------------------------------------------------------------
# Guarded local update apply
# ---------------------------------------------------------------------------

def _overlay_tree(src: Path, dst: Path) -> int:
    """Copy every file from a staged tree onto the target root; return count."""
    changed = 0
    for abs_path, rel in _iter_layer_files(src):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, target)
        changed += 1
    return changed


def _rollback_apply(pre_backup: Optional[LayerBackup], staged: Path, root: Path) -> None:
    """Undo a failed apply: revert modified files, then drop files it added."""
    if pre_backup is None:
        return
    restore_layer(pre_backup, target_root=root)
    with zipfile.ZipFile(pre_backup.path) as zf:
        pre_names = set(zf.namelist())
    for _, rel in _iter_layer_files(staged):
        if str(rel) not in pre_names:
            (root / rel).unlink(missing_ok=True)


def apply_update(
    staged_dir: Union[str, Path],
    *,
    layer: Layer = Layer.SEALED_CORE,
    home: Optional[Path] = None,
    backup: bool = True,
    timestamp: Optional[str] = None,
) -> UpdateResult:
    """Apply a staged local update to ``layer`` by overlaying ``staged_dir``.

    Modifying the sealed-core requires the self-modify toggle to be ON;
    otherwise :class:`SelfModifyBlocked` is raised and nothing is touched.
    A pre-apply backup is taken (unless ``backup=False``) and, if the overlay
    fails partway, the layer is rolled back to that snapshot before re-raising.
    """
    staged = Path(staged_dir)
    if not staged.is_dir():
        raise FileNotFoundError(f"staged update dir not found: {staged}")
    if layer is Layer.SEALED_CORE and not is_self_modify_enabled():
        raise SelfModifyBlocked(
            "self-modify is disabled — refusing to modify sealed-core; "
            "run `hermes layers unlock` to allow it"
        )

    root = layer_root(layer)
    pre_backup = backup_layer(layer, home=home, timestamp=timestamp) if backup else None
    try:
        changed = _overlay_tree(staged, root)
    except Exception:
        logger.warning("update apply failed on %s — rolling back", layer.value)
        _rollback_apply(pre_backup, staged, root)
        raise
    return UpdateResult(layer, root, changed, pre_backup)
