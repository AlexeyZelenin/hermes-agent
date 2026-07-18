from __future__ import annotations

import json
from pathlib import Path

from zeus_watchdog.config import default_home, load, resolve_chat_id, resolve_token


def test_defaults_anchor_under_home(tmp_path: Path):
    cfg = load(config_path=None, home=tmp_path)
    assert cfg.home == tmp_path
    assert cfg.kanban_db == tmp_path / "kanban" / "boards" / "ra" / "kanban.db"
    assert cfg.zeus_db == tmp_path / "zeus" / "zeus.db"
    assert cfg.chat_id_file == tmp_path / "zeus" / "telegram_chat_id"
    assert cfg.debounce_sec == 1800


def test_default_home_prefers_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_home() == tmp_path
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert default_home() == Path.home() / ".hermes"


def test_overlay_overrides_thresholds_and_paths(tmp_path: Path):
    overlay = {
        "debounce_sec": 60,
        "ready_no_run_sec": 30,
        "kanban_db": str(tmp_path / "custom.db"),
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")
    cfg = load(config_path=path, home=tmp_path)
    assert cfg.debounce_sec == 60
    assert cfg.ready_no_run_sec == 30
    assert cfg.kanban_db == tmp_path / "custom.db"
    # Unset keys keep defaults.
    assert cfg.heartbeat_timeout_sec == 7200


def test_resolve_token_inline_wins(tmp_path: Path):
    cfg = load(home=tmp_path)
    cfg.bot_token = "INLINE"
    assert resolve_token(cfg) == "INLINE"


def test_resolve_token_from_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        'OTHER=x\nexport TELEGRAM_BOT_TOKEN="123:abc"\nMORE=y\n', encoding="utf-8"
    )
    cfg = load(home=tmp_path)
    cfg.bot_token_env_file = env
    assert resolve_token(cfg) == "123:abc"


def test_resolve_token_missing_returns_empty(tmp_path: Path):
    cfg = load(home=tmp_path)
    cfg.bot_token_env_file = tmp_path / "nope.env"
    assert resolve_token(cfg) == ""


def test_resolve_chat_id_from_file(tmp_path: Path):
    cfg = load(home=tmp_path)
    cfg.chat_id_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.chat_id_file.write_text("125786270\n", encoding="utf-8")
    assert resolve_chat_id(cfg) == "125786270"
