"""Unit tests for the task-aware toolset selector (hermes_cli/toolset_selector).

The deterministic engine is pure (no I/O, no network), so these exercise it
directly. The aux stage is tested with a mocked ``call_llm``; the integration
seam (``_resolve_worker_cli_toolsets``) is covered in
test_kanban_worker_spawn_toolsets.py against an isolated HERMES_HOME.
"""
from __future__ import annotations

import pytest

from hermes_cli import toolset_selector as ts


# A generous ceiling covering every toolset the rules can target.
CEILING = [
    "file", "terminal", "todo", "web", "browser", "vision", "image_gen",
    "cronjob", "code_execution", "delegation", "session_search", "memory",
    "skills", "clarify", "homeassistant", "tts",
]


def _cfg(**kw) -> ts.SelectorConfig:
    base = dict(mode="narrow", denylist=())
    base.update(kw)
    return ts.SelectorConfig(**base)


def _sel(title="", body="", ceiling=None, config=None):
    return ts.select_toolsets(
        title=title, body=body,
        ceiling=list(CEILING if ceiling is None else ceiling),
        config=config or _cfg(),
    )


# --- mode off ---------------------------------------------------------------

def test_mode_off_returns_full_ceiling_unchanged():
    sel = _sel(title="build a website", config=ts.SelectorConfig(mode="off"))
    assert sel.toolsets == sorted(CEILING)
    assert sel.fallback == "mode_off"
    assert sel.dropped == []


def test_default_config_is_mode_off():
    assert ts.SelectorConfig().mode == "off"


# --- base set ---------------------------------------------------------------

def test_minimal_task_yields_base_only():
    sel = _sel(title="do the thing", body="just do it")
    assert sel.toolsets == sorted(["file", "terminal", "todo"])
    assert sel.base == sorted(["file", "terminal", "todo"])
    # everything else withheld
    assert "web" in sel.dropped and "browser" in sel.dropped


def test_base_intersected_with_ceiling_cannot_widen():
    # ceiling lacks "todo" — base must not smuggle it in
    sel = _sel(title="x", ceiling=["file", "terminal", "web"])
    assert "todo" not in sel.toolsets
    assert set(sel.toolsets) <= {"file", "terminal", "web"}


# --- keyword rules (EN + RU) ------------------------------------------------

def test_keyword_en_selects_web():
    sel = _sel(title="Search the documentation for the API")
    assert "web" in sel.toolsets


def test_keyword_ru_selects_web():
    sel = _sel(title="Найди в документации нужный раздел")
    assert "web" in sel.toolsets


def test_keyword_ru_delegation():
    sel = _sel(body="Разбей на подзадачи и делегируй параллельно")
    assert "delegation" in sel.toolsets


def test_keyword_selects_memory():
    sel = _sel(title="Запомни мои предпочтения по стилю")
    assert "memory" in sel.toolsets


# --- capability rules -------------------------------------------------------

def test_capability_url_selects_web():
    sel = _sel(body="Fetch data from https://example.com/api and parse it")
    assert "web" in sel.toolsets
    reasons = {m.toolset: m for m in sel.matched}
    assert "cap.url" in reasons["web"].rule_ids


def test_capability_browser_needs_url_and_verb():
    hit = _sel(body="Open https://example.com and click the login button")
    assert "browser" in hit.toolsets
    # url without an interaction verb should NOT trigger browser
    miss = _sel(body="See the docs at https://example.com for reference")
    assert "browser" not in miss.toolsets


def test_capability_image_ref_selects_vision():
    sel = _sel(body="Analyze the screenshot attached as diagram.png")
    assert "vision" in sel.toolsets


def test_capability_saturates_confidence():
    sel = _sel(body="Read https://example.com")
    web = {m.toolset: m for m in sel.matched}["web"]
    assert web.confidence == pytest.approx(1.0)


# --- denylist ---------------------------------------------------------------

def test_denylist_excludes_even_on_match():
    cfg = _cfg(denylist=("image_gen", "tts"))
    sel = _sel(title="generate a logo image and narrate audio", config=cfg)
    assert "image_gen" not in sel.toolsets
    assert "tts" not in sel.toolsets


def test_base_denylist_overlap_raises_at_load():
    cfg = {"kanban": {"toolset_selection": {
        "base": ["file", "terminal"], "denylist": ["terminal"]}}}
    with pytest.raises(ValueError):
        ts.load_selector_config(cfg)


# --- invariants -------------------------------------------------------------

def test_result_is_subset_of_ceiling():
    sel = _sel(title="generate image, search docs, run script, schedule cron",
               ceiling=["file", "terminal", "todo", "web"])
    assert set(sel.toolsets) <= {"file", "terminal", "todo", "web"}


def test_result_is_deduped_and_sorted():
    sel = _sel(title="Search docs", ceiling=["web", "web", "file", "terminal", "todo"])
    assert sel.toolsets == sorted(set(sel.toolsets))


def test_selection_is_deterministic():
    a = _sel(title="Search https://x.com and click login")
    b = _sel(title="Search https://x.com and click login")
    assert a.toolsets == b.toolsets


# --- thresholds -------------------------------------------------------------

def test_single_keyword_hit_clears_tau_select():
    # one keyword → score 1.0 → conf 0.5 == tau_select (0.5) → selected
    sel = _sel(title="use a skill for this")
    assert "skills" in sel.toolsets


def test_borderline_band_excluded_without_aux():
    # raise tau_select above a single-hit's 0.5 so it lands in [0.3, 0.6)
    cfg = _cfg(tau_select=0.6, tau_low=0.3, aux_enabled=False)
    sel = _sel(title="use a skill", config=cfg)
    assert "skills" not in sel.toolsets     # borderline → excluded (bias to minimal)
    assert "skills" in sel.borderline


# --- aux re-ranking ---------------------------------------------------------

def _borderline_cfg(**kw):
    # tau_select 0.6 forces single-keyword hits (conf 0.5) into the borderline band
    return _cfg(tau_select=0.6, tau_low=0.3, aux_enabled=True, **kw)


def test_aux_promotes_borderline(monkeypatch):
    class _Msg:
        content = '{"include": ["skills"]}'

    class _Resp:
        choices = [type("C", (), {"message": _Msg()})()]

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm", lambda *a, **k: _Resp())
    sel = _sel(title="use a skill", config=_borderline_cfg())
    assert "skills" in sel.toolsets
    assert sel.aux_used is True
    assert "skills" in sel.aux_delta


def test_aux_narrow_only_discards_out_of_band(monkeypatch):
    # aux tries to add a non-candidate (image_gen) — must be discarded
    class _Msg:
        content = '{"include": ["skills", "image_gen", "spotify"]}'

    class _Resp:
        choices = [type("C", (), {"message": _Msg()})()]

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm", lambda *a, **k: _Resp())
    sel = _sel(title="use a skill", config=_borderline_cfg())
    assert "skills" in sel.toolsets
    assert "image_gen" not in sel.toolsets
    assert "spotify" not in sel.toolsets


def test_aux_failure_falls_back_to_deterministic(monkeypatch):
    def _boom(*a, **k):
        raise TimeoutError("aux backend dead")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _boom)
    sel = _sel(title="use a skill", config=_borderline_cfg())
    assert sel.aux_used is False
    assert "skills" not in sel.toolsets     # borderline stays excluded on failure
    assert sel.fallback is None             # deterministic result is not an error


def test_aux_disabled_never_calls(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("call_llm must not be invoked when aux disabled")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _boom)
    sel = _sel(title="use a skill", config=_cfg(tau_select=0.6, aux_enabled=False))
    assert sel.aux_used is False


# --- fallback on error ------------------------------------------------------

def test_internal_error_fails_open_to_ceiling(monkeypatch):
    monkeypatch.setattr(
        ts, "_select_narrow",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    sel = _sel(title="Search docs")
    assert sel.toolsets == sorted(ts._dedupe(CEILING))
    assert sel.fallback == "selector_error:RuntimeError"


# --- config loading ---------------------------------------------------------

def test_load_selector_config_defaults():
    cfg = ts.load_selector_config({})
    assert cfg.mode == "off"
    assert cfg.base == ts.DEFAULT_BASE


def test_load_selector_config_board_override():
    cfg = ts.load_selector_config(
        {"kanban": {"toolset_selection": {"mode": "off"}}},
        board_overrides={"mode": "narrow"})
    assert cfg.mode == "narrow"


def test_load_selector_config_invalid_mode_defaults_off():
    cfg = ts.load_selector_config({"kanban": {"toolset_selection": {"mode": "bogus"}}})
    assert cfg.mode == "off"


# --- observability ----------------------------------------------------------

def test_emit_selection_log_never_raises():
    sel = _sel(title="Search docs")
    ts.emit_selection_log(object(), "ra", sel)   # bare object → getattr defaults
