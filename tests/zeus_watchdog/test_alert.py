from __future__ import annotations

from zeus_watchdog import alert as alert_mod
from zeus_watchdog.alert import dispatch, format_message
from zeus_watchdog.state import PROBLEM, RECOVERY, Alert


def test_format_problem_and_recovery():
    assert format_message(Alert(PROBLEM, "k", "gateway мёртв")) == (
        "⚠️ Zeus нужно внимание: gateway мёртв"
    )
    assert format_message(Alert(RECOVERY, "k", "gateway мёртв")) == (
        "✅ Zeus ожил: gateway мёртв"
    )


def test_dispatch_uses_injected_sender(cfg):
    sent = []

    def fake(token, chat_id, text):
        sent.append((token, chat_id, text))
        return True

    n = dispatch(cfg, [Alert(PROBLEM, "k", "boom")], sender=fake)
    assert n == 1
    assert sent == [("TESTTOKEN", "999", "⚠️ Zeus нужно внимание: boom")]


def test_dispatch_skips_when_token_missing(cfg):
    cfg.bot_token = ""
    cfg.bot_token_env_file = cfg.home / "does-not-exist.env"
    calls = []
    n = dispatch(cfg, [Alert(PROBLEM, "k", "boom")], sender=lambda *a: calls.append(a) or True)
    assert n == 0
    assert calls == []


def test_dispatch_counts_only_delivered(cfg):
    def flaky(token, chat_id, text):
        return "ok" in text  # only recovery messages "succeed" here

    n = dispatch(
        cfg,
        [Alert(PROBLEM, "k1", "bad"), Alert(RECOVERY, "k2", "ok now")],
        sender=flaky,
    )
    assert n == 1


def test_curl_sender_builds_expected_argv(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "200"

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(alert_mod.subprocess, "run", fake_run)
    ok = alert_mod.curl_sender("TOK", "42", "hello")
    assert ok is True
    argv = captured["argv"]
    assert argv[0] == "curl"
    assert "https://api.telegram.org/botTOK/sendMessage" in argv
    assert "chat_id=42" in argv
    assert "text=hello" in argv
