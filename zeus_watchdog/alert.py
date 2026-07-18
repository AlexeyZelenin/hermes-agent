"""Telegram alerting via a direct ``curl`` to the Bot API.

Kept deliberately dumb: format a short message, POST it. The transport is
injectable (``sender``) so tests never touch the network, and the token is
resolved lazily and never logged.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Callable

from .config import Config, resolve_chat_id, resolve_token
from .state import RECOVERY, Alert

# sender(token, chat_id, text) -> bool (delivered ok)
Sender = Callable[[str, str, str], bool]

_API = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(alert: Alert) -> str:
    if alert.kind == RECOVERY:
        return f"✅ Zeus ожил: {alert.summary}"
    return f"⚠️ Zeus нужно внимание: {alert.summary}"


def curl_sender(token: str, chat_id: str, text: str) -> bool:
    """POST to the Bot API via curl. Returns True on a 0 exit code."""
    url = _API.format(token=token)
    try:
        proc = subprocess.run(
            [
                "curl", "-sS", "--max-time", "15", "-X", "POST", url,
                "--data-urlencode", f"chat_id={chat_id}",
                "--data-urlencode", f"text={text}",
                "-o", "/dev/null", "-w", "%{http_code}",
            ],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"watchdog: curl failed: {exc}", file=sys.stderr)
        return False
    ok = proc.returncode == 0 and proc.stdout.strip().startswith("2")
    if not ok:
        print(
            f"watchdog: telegram send returned http={proc.stdout.strip()} rc={proc.returncode}",
            file=sys.stderr,
        )
    return ok


def dispatch(cfg: Config, alerts: list[Alert], sender: Sender = curl_sender) -> int:
    """Send every alert. Returns the count actually delivered."""
    if not alerts:
        return 0
    token = resolve_token(cfg)
    chat_id = resolve_chat_id(cfg)
    if not token or not chat_id:
        missing = "token" if not token else "chat_id"
        print(f"watchdog: cannot alert — Telegram {missing} unresolved", file=sys.stderr)
        return 0
    sent = 0
    for alert in alerts:
        if sender(token, chat_id, format_message(alert)):
            sent += 1
    return sent
