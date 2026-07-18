"""Tests for the sealed-core / userland layer model, self-modify toggle,
guarded local update apply, and two-layer backup + restore.

Every test isolates both layers into ``tmp_path``:

  * userland  → ``HERMES_HOME`` env pointed at a temp dir
  * sealed-core → ``HERMES_SEALED_CORE_ROOT`` env pointed at a temp dir

so nothing touches the developer's real ``~/.hermes`` or the live code tree.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hermes_cli import system_layers as sl
from hermes_cli.system_layers import Layer, SelfModifyBlocked, LayerBackupNotFound


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def layers(tmp_path, monkeypatch):
    """Isolated userland + sealed-core roots with realistic seed content."""
    home = tmp_path / "userland"
    core = tmp_path / "sealed-core"
    home.mkdir()
    core.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_SEALED_CORE_ROOT", str(core))
    # Default the toggle to a clean, unset state for each test.
    monkeypatch.delenv("HERMES_SELF_MODIFY", raising=False)

    # sealed-core: code files + a regeneratable dir that must be excluded
    (core / "cli.py").write_text("VERSION = 1\n")
    (core / "agent").mkdir()
    (core / "agent" / "loop.py").write_text("def run():\n    return 1\n")
    (core / "node_modules").mkdir()
    (core / "node_modules" / "junk.js").write_text("x")

    # userland: state files
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
    (home / "memory").mkdir()
    (home / "memory" / "note.md").write_text("remember this\n")

    return {"home": home, "core": core}


def _staged_update(tmp_path: Path) -> Path:
    """A staged local update that bumps cli.py and adds a new module."""
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "cli.py").write_text("VERSION = 2\n")
    (staged / "agent").mkdir()
    (staged / "agent" / "new_feature.py").write_text("FEATURE = True\n")
    return staged


# ---------------------------------------------------------------------------
# AC #1 — layers are physically/logically separate and independently addressed
# ---------------------------------------------------------------------------

def test_layers_addressed_independently(layers):
    assert sl.sealed_core_root() == layers["core"]
    assert sl.userland_root() == layers["home"]
    assert sl.layer_root(Layer.SEALED_CORE) == layers["core"]
    assert sl.layer_root(Layer.USERLAND) == layers["home"]
    assert sl.sealed_core_root() != sl.userland_root()


# ---------------------------------------------------------------------------
# self-modify toggle
# ---------------------------------------------------------------------------

def test_self_modify_default_locked(layers):
    assert sl.is_self_modify_enabled() is False


def test_self_modify_config_roundtrip(layers):
    sl.set_self_modify(True)
    assert sl.is_self_modify_enabled() is True
    sl.set_self_modify(False)
    assert sl.is_self_modify_enabled() is False


def test_self_modify_env_override_wins(layers, monkeypatch):
    sl.set_self_modify(False)
    monkeypatch.setenv("HERMES_SELF_MODIFY", "1")
    assert sl.is_self_modify_enabled() is True
    monkeypatch.setenv("HERMES_SELF_MODIFY", "off")
    assert sl.is_self_modify_enabled() is False


# ---------------------------------------------------------------------------
# AC #3 — sealed-core update blocked while self-modify is OFF
# ---------------------------------------------------------------------------

def test_update_blocked_when_locked(layers, tmp_path):
    staged = _staged_update(tmp_path)
    sl.set_self_modify(False)

    with pytest.raises(SelfModifyBlocked):
        sl.apply_update(staged, layer=Layer.SEALED_CORE)

    # Nothing changed and no backup was written.
    assert (layers["core"] / "cli.py").read_text() == "VERSION = 1\n"
    assert not (layers["core"] / "agent" / "new_feature.py").exists()
    assert sl.list_backups() == []


# ---------------------------------------------------------------------------
# AC #4 — sealed-core update succeeds while self-modify is ON
# ---------------------------------------------------------------------------

def test_update_applies_when_unlocked(layers, tmp_path):
    staged = _staged_update(tmp_path)
    sl.set_self_modify(True)

    result = sl.apply_update(staged, layer=Layer.SEALED_CORE)

    assert result.layer is Layer.SEALED_CORE
    assert result.changed == 2
    assert (layers["core"] / "cli.py").read_text() == "VERSION = 2\n"
    assert (layers["core"] / "agent" / "new_feature.py").read_text() == "FEATURE = True\n"
    assert result.backup is not None  # pre-update snapshot exists


# ---------------------------------------------------------------------------
# AC #2 — userland is never touched by a sealed-core update
# ---------------------------------------------------------------------------

def test_userland_untouched_by_core_update(layers, tmp_path):
    staged = _staged_update(tmp_path)
    sl.set_self_modify(True)

    # Snapshot userland state *after* the toggle write (a legitimate userland
    # change) and *before* the sealed-core apply — the apply must not alter it.
    config_before = (layers["home"] / "config.yaml").read_text()
    sl.apply_update(staged, layer=Layer.SEALED_CORE)

    assert (layers["home"] / "config.yaml").read_text() == config_before
    assert (layers["home"] / "memory" / "note.md").read_text() == "remember this\n"


# ---------------------------------------------------------------------------
# AC #5 — backup covers both layers; excludes regeneratable dirs
# ---------------------------------------------------------------------------

def test_backup_all_covers_both_layers(layers):
    refs = sl.backup_all()
    covered = {ref.layer for ref in refs}
    assert covered == {Layer.SEALED_CORE, Layer.USERLAND}
    for ref in refs:
        assert ref.path.exists()

    core_ref = next(r for r in refs if r.layer is Layer.SEALED_CORE)
    with zipfile.ZipFile(core_ref.path) as zf:
        names = set(zf.namelist())
    assert "cli.py" in names
    # node_modules is regeneratable and must be excluded
    assert not any(n.startswith("node_modules/") for n in names)


def test_list_backups_reads_manifests(layers):
    sl.backup_all()
    listed = sl.list_backups()
    assert len(listed) == 2
    assert {r.layer for r in listed} == {Layer.SEALED_CORE, Layer.USERLAND}


# ---------------------------------------------------------------------------
# AC #6 — restore after a failed update returns a working state
# ---------------------------------------------------------------------------

def test_restore_after_failed_update_rolls_back(layers, tmp_path, monkeypatch):
    """A mid-apply failure must auto-roll-back to the pre-update snapshot:
    modified files reverted AND files the bad update added removed."""
    staged = _staged_update(tmp_path)
    sl.set_self_modify(True)

    original_overlay = sl._overlay_tree
    calls = {"n": 0}

    def _boom(src, dst):
        # Let the overlay partially apply, then blow up like a real failure.
        original_overlay(src, dst)
        calls["n"] += 1
        raise RuntimeError("simulated apply failure")

    monkeypatch.setattr(sl, "_overlay_tree", _boom)

    with pytest.raises(RuntimeError, match="simulated apply failure"):
        sl.apply_update(staged, layer=Layer.SEALED_CORE)

    # Modified file reverted, added file removed → back to a working state.
    assert (layers["core"] / "cli.py").read_text() == "VERSION = 1\n"
    assert not (layers["core"] / "agent" / "new_feature.py").exists()
    assert (layers["core"] / "agent" / "loop.py").read_text() == "def run():\n    return 1\n"


def test_manual_restore_clean_removes_untracked(layers):
    """Explicit snapshot restore (clean=True) undoes post-backup additions."""
    sl.set_self_modify(True)
    ref = sl.backup_layer(Layer.SEALED_CORE)
    assert ref is not None

    # Simulate drift after the backup: change a file, add a new one.
    (layers["core"] / "cli.py").write_text("VERSION = 99\n")
    (layers["core"] / "rogue.py").write_text("bad\n")

    result = sl.restore_layer(ref, clean=True)

    assert (layers["core"] / "cli.py").read_text() == "VERSION = 1\n"
    assert not (layers["core"] / "rogue.py").exists()
    assert result.removed == 1


def test_restore_unknown_id_raises(layers):
    with pytest.raises(LayerBackupNotFound):
        sl.restore_layer("sealed-core-nonexistent")


# ---------------------------------------------------------------------------
# Userland updates are always allowed (no toggle gate)
# ---------------------------------------------------------------------------

def test_userland_update_needs_no_toggle(layers, tmp_path):
    staged = tmp_path / "staged_userland"
    staged.mkdir()
    (staged / "config.yaml").write_text("model:\n  provider: anthropic\n")
    sl.set_self_modify(False)  # locked — must not block userland

    result = sl.apply_update(staged, layer=Layer.USERLAND, backup=False)

    assert result.changed == 1
    assert (layers["home"] / "config.yaml").read_text() == "model:\n  provider: anthropic\n"
