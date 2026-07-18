"""Watchdog configuration: defaults, JSON overlay, and secret resolution.

Everything is a plain path/number so the watchdog needs no YAML dependency and
no ``hermes`` import. A user config file (JSON) overlays the defaults; unknown
keys are ignored. The Telegram token is never stored in state or logged — it is
resolved lazily from either an inline value or a ``KEY=value`` line in an
env-style file (``~/.hermes/.env`` by default).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_home() -> Path:
    """Resolve the hermes root: ``$HERMES_HOME`` else ``~/.hermes``."""
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".hermes"


@dataclass
class Config:
    """Resolved watchdog configuration. All paths are absolute."""

    home: Path
    gateway_pid_file: Path
    gateway_log: Path
    kanban_db: Path
    zeus_db: Path
    chat_id_file: Path
    state_file: Path
    bot_token_env_file: Path
    bot_token_key: str = "TELEGRAM_BOT_TOKEN"
    bot_token: str = ""  # inline override; empty means read from env file
    chat_id: str = ""  # inline override; empty means read from chat_id_file

    # Thresholds (seconds).
    dispatcher_log_stale_sec: int = 600
    ready_no_run_sec: int = 600
    # Must exceed the SLOWEST executor's heartbeat cadence: ACP workers
    # (claude-code) only touch last_heartbeat_at ~hourly, so a healthy long
    # session shows ~70min gaps. 2h avoids false positives while still catching
    # a truly dead worker. Tune down if you run only fast-heartbeat workers.
    heartbeat_timeout_sec: int = 7200
    resume_grace_sec: int = 300
    recent_limit_window_sec: int = 6 * 3600
    debounce_sec: int = 1800
    interval_sec: int = 120

    # Fields the JSON overlay may set directly (name -> is_path).
    _PATH_KEYS = (
        "gateway_pid_file", "gateway_log", "kanban_db", "zeus_db",
        "chat_id_file", "state_file", "bot_token_env_file",
    )
    _STR_KEYS = ("bot_token_key", "bot_token", "chat_id")
    _INT_KEYS = (
        "dispatcher_log_stale_sec", "ready_no_run_sec", "heartbeat_timeout_sec",
        "resume_grace_sec", "recent_limit_window_sec", "debounce_sec", "interval_sec",
    )


def _defaults(home: Path) -> dict[str, Any]:
    return {
        "home": home,
        "gateway_pid_file": home / "gateway.pid",
        "gateway_log": home / "logs" / "gateway.log",
        "kanban_db": home / "kanban" / "boards" / "ra" / "kanban.db",
        "zeus_db": home / "zeus" / "zeus.db",
        "chat_id_file": home / "zeus" / "telegram_chat_id",
        "state_file": home / "zeus" / "watchdog.state.json",
        "bot_token_env_file": home / ".env",
    }


def load(config_path: Path | None = None, home: Path | None = None) -> Config:
    """Build a :class:`Config` from defaults overlaid with an optional JSON file."""
    home = (home or default_home()).expanduser()
    values = _defaults(home)
    overlay: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        overlay = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if "home" in overlay:
            home = Path(str(overlay["home"])).expanduser()
            values = _defaults(home)  # re-derive path defaults under the new home

    for key in Config._PATH_KEYS:
        if key in overlay and overlay[key]:
            values[key] = Path(str(overlay[key])).expanduser()
    extra: dict[str, Any] = {}
    for key in Config._STR_KEYS:
        if key in overlay and overlay[key] is not None:
            extra[key] = str(overlay[key])
    for key in Config._INT_KEYS:
        if key in overlay and overlay[key] is not None:
            extra[key] = int(overlay[key])
    return Config(**values, **extra)


def resolve_token(cfg: Config) -> str:
    """Return the Telegram bot token, or '' if it cannot be resolved.

    Never logs the token. Reads a single ``KEY=value`` line from the env file
    without importing anything or evaluating the file as a shell script.
    """
    if cfg.bot_token.strip():
        return cfg.bot_token.strip()
    path = cfg.bot_token_env_file
    if not path.is_file():
        return ""
    prefix = cfg.bot_token_key + "="
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"').strip("'")
    return ""


def resolve_chat_id(cfg: Config) -> str:
    """Return the Telegram chat id from the inline override or the id file."""
    if cfg.chat_id.strip():
        return cfg.chat_id.strip()
    if cfg.chat_id_file.is_file():
        return cfg.chat_id_file.read_text(encoding="utf-8").strip()
    return ""
