"""Per-project memory scope isolation.

Covers the seam that keeps preferences (MEMORY.md / USER.md) separate per
project so a fact saved while working in project A is invisible in project B:

  - ``coding_context.project_scope_key`` / ``RuntimeMode.memory_scope`` — how a
    working directory maps to a stable, collision-resistant scope key, gated on
    the coding posture's ``memory_policy == "project"``.
  - ``memory_tool.get_memory_dir`` — how the active scope redirects the store to
    a project-isolated directory, with global as the default.
  - end-to-end isolation through ``MemoryStore`` across two projects.

Storage is confined to the per-test ``HERMES_HOME`` tempdir by the autouse
``_hermetic_environment`` fixture in ``tests/conftest.py``.
"""

import pytest

from agent import coding_context
from tools import memory_tool
from tools.memory_tool import (
    MemoryStore,
    get_memory_dir,
    get_memory_scope,
    set_memory_scope,
    reset_memory_scope,
)


@pytest.fixture(autouse=True)
def _reset_scope():
    """Ensure the process-wide memory-scope ContextVar can't leak across tests."""
    token = set_memory_scope(None)
    try:
        yield
    finally:
        reset_memory_scope(token)


def _make_project(base, name, marker="pyproject.toml"):
    """A directory that ``coding_context`` recognises as a project root."""
    root = base / name
    root.mkdir(parents=True)
    (root / marker).write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return root


# =========================================================================
# Scope key derivation (coding_context)
# =========================================================================

class TestProjectScopeKey:
    def test_none_outside_any_project(self, tmp_path):
        plain = tmp_path / "just_a_folder"
        plain.mkdir()
        assert coding_context.project_scope_key(plain) is None

    def test_key_for_project_root(self, tmp_path):
        root = _make_project(tmp_path, "alpha")
        key = coding_context.project_scope_key(root)
        assert key is not None
        # Human-readable basename prefix + hash suffix, filesystem-safe.
        assert key.startswith("alpha-")
        assert "/" not in key and " " not in key

    def test_stable_for_same_root(self, tmp_path):
        root = _make_project(tmp_path, "alpha")
        assert coding_context.project_scope_key(root) == coding_context.project_scope_key(root)

    def test_subdir_resolves_to_same_project(self, tmp_path):
        root = _make_project(tmp_path, "alpha")
        sub = root / "pkg" / "inner"
        sub.mkdir(parents=True)
        assert coding_context.project_scope_key(sub) == coding_context.project_scope_key(root)

    def test_distinct_roots_distinct_keys(self, tmp_path):
        a = _make_project(tmp_path, "alpha")
        b = _make_project(tmp_path, "beta")
        assert coding_context.project_scope_key(a) != coding_context.project_scope_key(b)

    def test_same_basename_different_path_no_collision(self, tmp_path):
        """Two projects that share a name in different dirs must not collide."""
        a = _make_project(tmp_path / "x", "shared")
        b = _make_project(tmp_path / "y", "shared")
        assert coding_context.project_scope_key(a) != coding_context.project_scope_key(b)


# =========================================================================
# RuntimeMode.memory_scope — gated on the coding posture
# =========================================================================

class TestRuntimeModeMemoryScope:
    def test_coding_posture_in_project_scopes(self, tmp_path):
        root = _make_project(tmp_path, "alpha")
        mode = coding_context.resolve_runtime_mode(platform="cli", cwd=root)
        assert mode.is_coding
        assert mode.memory_scope() == coding_context.project_scope_key(root)

    def test_general_posture_stays_global(self, tmp_path):
        plain = tmp_path / "notes"
        plain.mkdir()
        mode = coding_context.resolve_runtime_mode(platform="cli", cwd=plain)
        assert not mode.is_coding
        assert mode.memory_scope() is None

    def test_coding_disabled_stays_global(self, tmp_path):
        root = _make_project(tmp_path, "alpha")
        mode = coding_context.resolve_runtime_mode(
            platform="cli", cwd=root, config={"agent": {"coding_context": "off"}}
        )
        assert mode.memory_scope() is None


# =========================================================================
# get_memory_dir — scope redirects the store directory
# =========================================================================

class TestMemoryDirScope:
    def test_global_by_default(self):
        from hermes_constants import get_hermes_home

        assert get_memory_scope() is None
        assert get_memory_dir() == get_hermes_home() / "memories"

    def test_scoped_dir(self):
        from hermes_constants import get_hermes_home

        set_memory_scope("alpha-abc12345")
        expected = get_hermes_home() / "memories" / "projects" / "alpha-abc12345"
        assert get_memory_dir() == expected

    def test_reset_restores_global(self):
        from hermes_constants import get_hermes_home

        token = set_memory_scope("alpha-abc12345")
        assert get_memory_dir() != get_hermes_home() / "memories"
        reset_memory_scope(token)
        assert get_memory_dir() == get_hermes_home() / "memories"


# =========================================================================
# End-to-end isolation across two projects (the acceptance criteria)
# =========================================================================

class TestCrossProjectIsolation:
    def _store(self):
        store = MemoryStore()
        store.load_from_disk()
        return store

    def test_pref_saved_in_A_not_visible_in_B(self):
        # Project A: save a preference.
        set_memory_scope("projA-11111111")
        a = self._store()
        a.add("user", "User prefers tabs over spaces")
        assert any("tabs over spaces" in e for e in self._store().user_entries)

        # Project B: fresh scope — the preference must not be present.
        set_memory_scope("projB-22222222")
        assert not any("tabs over spaces" in e for e in self._store().user_entries)

    def test_switching_scope_switches_prefs_without_manual_reset(self):
        set_memory_scope("projA-11111111")
        self._store().add("user", "A: dark mode")

        set_memory_scope("projB-22222222")
        self._store().add("user", "B: light mode")

        # Switching back surfaces A's set and none of B's — no manual state reset.
        set_memory_scope("projA-11111111")
        entries = self._store().user_entries
        assert any("dark mode" in e for e in entries)
        assert not any("light mode" in e for e in entries)

        set_memory_scope("projB-22222222")
        entries = self._store().user_entries
        assert any("light mode" in e for e in entries)
        assert not any("dark mode" in e for e in entries)

    def test_update_and_remove_affect_only_current_scope(self):
        set_memory_scope("projA-11111111")
        self._store().add("user", "shared-looking entry from A")
        set_memory_scope("projB-22222222")
        self._store().add("user", "shared-looking entry from B")

        # Replace in A, then remove in A — B is untouched throughout.
        set_memory_scope("projA-11111111")
        a = self._store()
        a.replace("user", "from A", "rewritten entry from A")
        assert any("rewritten entry from A" in e for e in self._store().user_entries)

        set_memory_scope("projB-22222222")
        b_entries = self._store().user_entries
        assert any("shared-looking entry from B" in e for e in b_entries)
        assert not any("rewritten entry from A" in e for e in b_entries)

        set_memory_scope("projA-11111111")
        a = self._store()
        a.remove("user", "rewritten entry from A")
        assert not any("from A" in e for e in self._store().user_entries)

        set_memory_scope("projB-22222222")
        assert any("shared-looking entry from B" in e for e in self._store().user_entries)

    def test_project_and_global_are_isolated(self):
        # A global write (no scope) is invisible in a project scope and vice versa.
        set_memory_scope(None)
        self._store().add("memory", "global fact")

        set_memory_scope("projA-11111111")
        assert not any("global fact" in e for e in self._store().memory_entries)
        self._store().add("memory", "project A fact")

        set_memory_scope(None)
        entries = self._store().memory_entries
        assert any("global fact" in e for e in entries)
        assert not any("project A fact" in e for e in entries)

    def test_scoped_files_live_under_projects_subtree(self):
        from hermes_constants import get_hermes_home

        set_memory_scope("projA-11111111")
        self._store().add("user", "lands in the scoped dir")
        scoped = get_hermes_home() / "memories" / "projects" / "projA-11111111" / "USER.md"
        assert scoped.exists()
        # The global store stays empty.
        assert not (get_hermes_home() / "memories" / "USER.md").exists()
