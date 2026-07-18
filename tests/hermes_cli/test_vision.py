"""Tests for hermes_cli.vision — vision-doc resolution and loading.

No network, no board: pure file resolution against tmp dirs and env overrides.
"""

from __future__ import annotations

from hermes_cli import vision as vision_mod


def _write(root, text="# Vision\n\nbody"):
    d = root / "knowledge"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "vision.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_explicit_override_wins(tmp_path, monkeypatch):
    p = tmp_path / "custom-vision.md"
    p.write_text("# Custom", encoding="utf-8")
    monkeypatch.setenv("HERMES_VISION_DOC", str(p))
    assert vision_mod.vision_doc_path() == p
    assert vision_mod.load_vision_text() == "# Custom"


def test_override_pointing_at_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_VISION_DOC", str(tmp_path / "nope.md"))
    assert vision_mod.vision_doc_path() is None
    assert vision_mod.load_vision_text() is None


def test_project_root_env_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_VISION_DOC", raising=False)
    p = _write(tmp_path)
    monkeypatch.setenv("HERMES_PROJECT_ROOT", str(tmp_path))
    assert vision_mod.vision_doc_path() == p


def test_explicit_project_root_arg_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_VISION_DOC", raising=False)
    monkeypatch.delenv("HERMES_PROJECT_ROOT", raising=False)
    p = _write(tmp_path)
    assert vision_mod.vision_doc_path(project_root=tmp_path) == p
    assert vision_mod.load_vision_text(project_root=tmp_path).startswith("# Vision")


def test_truncation_caps_length(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_VISION_DOC", raising=False)
    monkeypatch.delenv("HERMES_PROJECT_ROOT", raising=False)
    _write(tmp_path, text="x" * 5000)
    out = vision_mod.load_vision_text(project_root=tmp_path, max_chars=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_empty_doc_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_VISION_DOC", raising=False)
    monkeypatch.delenv("HERMES_PROJECT_ROOT", raising=False)
    _write(tmp_path, text="   \n  ")
    assert vision_mod.load_vision_text(project_root=tmp_path) is None


def test_shipping_repo_has_a_vision_doc(monkeypatch):
    # The repo this code ships in must carry knowledge/vision.md (acceptance
    # criterion: the file exists with the required sections).
    monkeypatch.delenv("HERMES_VISION_DOC", raising=False)
    monkeypatch.delenv("HERMES_PROJECT_ROOT", raising=False)
    text = vision_mod.load_vision_text()
    assert text is not None
    for heading in ("Что строим", "Текущие фичи", "Куда идём", "Что НЕ делаем"):
        assert heading in text
