"""Focused regressions for the Copilot ACP shim safety layer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.copilot_acp_client import CopilotACPClient, _canonical_turn_usage


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


class CopilotACPClientSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")

    def test_extracted_tool_calls_match_openai_sdk_shape(self) -> None:
        tool_response = (
            "I'll inspect that.\n"
            "<tool_call>"
            '{"id":"call_read","type":"function",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}'
            "</tool_call>"
        )

        with patch.object(self.client, "_run_prompt", return_value=(tool_response, "")):
            response = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "read README.md"}],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    }
                ],
            )

        choice = response.choices[0]
        self.assertEqual(choice.finish_reason, "tool_calls")
        tool_call = choice.message.tool_calls[0]
        self.assertEqual(tool_call.id, "call_read")
        self.assertEqual(tool_call.function.name, "read_file")
        self.assertEqual(
            json.loads(tool_call.function.arguments),
            {"path": "README.md"},
        )
        self.assertEqual(dict(tool_call)["id"], "call_read")
        self.assertEqual(dict(tool_call.function)["name"], "read_file")
        self.assertEqual(choice.message.content, "I'll inspect that.")

    def test_stream_true_returns_iterable_text_chunks(self) -> None:
        with patch.object(self.client, "_run_prompt", return_value=("Hello from ACP", "")):
            stream = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
            )

        chunks = list(stream)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].choices[0].delta.content, "Hello from ACP")
        self.assertIsNone(chunks[0].choices[0].delta.tool_calls)
        self.assertEqual(chunks[0].choices[0].finish_reason, "stop")
        self.assertEqual(chunks[1].choices, [])
        self.assertEqual(chunks[1].usage.total_tokens, 0)

    def test_stream_true_preserves_tool_call_deltas(self) -> None:
        tool_response = (
            "<tool_call>"
            '{"id":"call_read","type":"function",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}'
            "</tool_call>"
        )

        with patch.object(self.client, "_run_prompt", return_value=(tool_response, "")):
            stream = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "read README.md"}],
                stream=True,
            )

        chunks = list(stream)
        delta = chunks[0].choices[0].delta
        self.assertIsNone(delta.content)
        self.assertEqual(chunks[0].choices[0].finish_reason, "tool_calls")
        self.assertEqual(len(delta.tool_calls), 1)
        tool_delta = delta.tool_calls[0]
        self.assertEqual(tool_delta.index, 0)
        self.assertEqual(tool_delta.id, "call_read")
        self.assertEqual(tool_delta.function.name, "read_file")
        self.assertEqual(
            json.loads(tool_delta.function.arguments),
            {"path": "README.md"},
        )
        self.assertEqual(chunks[1].choices, [])

    def test_timeout_object_is_coerced_for_streaming_requests(self) -> None:
        captured: dict[str, float] = {}

        def fake_run_prompt(prompt_text: str, *, timeout_seconds: float) -> tuple[str, str]:
            captured["timeout"] = timeout_seconds
            return "ok", ""

        timeout = type(
            "TimeoutLike",
            (),
            {"read": 12.0, "write": 5.0, "connect": 3.0, "pool": 1.0},
        )()

        with patch.object(self.client, "_run_prompt", side_effect=fake_run_prompt):
            list(
                self.client._create_chat_completion(
                    model="copilot-acp",
                    messages=[{"role": "user", "content": "hello"}],
                    timeout=timeout,
                    stream=True,
                )
            )

        self.assertEqual(captured["timeout"], 12.0)

    def _dispatch(self, message: dict, *, cwd: str) -> dict:
        process = _FakeProcess()
        handled = self.client._handle_server_message(
            message,
            process=process,
            cwd=cwd,
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        payload = process.stdin.getvalue().strip()
        self.assertTrue(payload)
        return json.loads(payload)

    def test_usage_update_notification_is_captured(self) -> None:
        handled = self.client._handle_server_message(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 1234,
                        "size": 200000,
                    }
                },
            },
            process=_FakeProcess(),
            cwd="/tmp",
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        self.assertEqual(self.client._last_usage_update["used"], 1234)

    def test_canonical_turn_usage_prefers_prompt_result_usage(self) -> None:
        usage = _canonical_turn_usage(
            {
                "inputTokens": 10,
                "outputTokens": 5,
                "cachedReadTokens": 2,
                "cachedWriteTokens": 1,
                "totalTokens": 18,
            },
            {"used": 999},
        )
        self.assertEqual(
            usage,
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 2,
                "cache_write_tokens": 1,
                "total_tokens": 18,
            },
        )

    def test_canonical_turn_usage_falls_back_to_usage_update(self) -> None:
        usage = _canonical_turn_usage(None, {"used": 4321, "size": 200000})
        self.assertEqual(usage["total_tokens"], 4321)
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["output_tokens"], 0)

    def test_canonical_turn_usage_returns_none_without_signal(self) -> None:
        self.assertIsNone(_canonical_turn_usage(None, None))
        self.assertIsNone(_canonical_turn_usage({"inputTokens": 0}, {"used": 0}))

    def test_request_permission_is_not_auto_allowed(self) -> None:
        response = self._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/request_permission",
                "params": {},
            },
            cwd="/tmp",
        )

        outcome = (((response.get("result") or {}).get("outcome") or {}).get("outcome"))
        self.assertEqual(outcome, "cancelled")

    def test_read_text_file_blocks_internal_hermes_hub_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            blocked = home / ".hermes" / "skills" / ".hub" / "index-cache" / "entry.json"
            blocked.parent.mkdir(parents=True, exist_ok=True)
            blocked.write_text('{"token":"sk-test-secret-1234567890"}')

            with patch.dict(
                os.environ,
                {"HOME": str(home), "HERMES_HOME": str(home / ".hermes")},
                clear=False,
            ):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "fs/read_text_file",
                        "params": {"path": str(blocked)},
                    },
                    cwd=str(home),
                )

        self.assertIn("error", response)

    def test_read_text_file_redacts_sensitive_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_file = root / "config.env"
            secret_file.write_text("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")

            # agent.redact snapshots HERMES_REDACT_SECRETS at import time into
            # _REDACT_ENABLED, so patching os.environ is a no-op. Flip the
            # module-level constant directly for the duration of the call.
            with patch("agent.redact._REDACT_ENABLED", True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "fs/read_text_file",
                        "params": {"path": str(secret_file)},
                    },
                    cwd=str(root),
                )

        content = ((response.get("result") or {}).get("content") or "")
        self.assertNotIn("abc123def456", content)
        self.assertIn("OPENAI_API_KEY=", content)

    def test_write_text_file_reuses_write_denylist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            target = home / ".ssh" / "id_rsa"
            target.parent.mkdir(parents=True, exist_ok=True)

            with patch(
                "agent.copilot_acp_client.get_write_denied_error",
                return_value="Write denied: protected",
                create=True,
            ):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(target),
                            "content": "fake-private-key",
                        },
                    },
                    cwd=str(home),
                )

        self.assertIn("error", response)
        self.assertFalse(target.exists())

    def test_write_text_file_respects_safe_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            safe_root = root / "workspace"
            safe_root.mkdir()
            outside = root / "outside.txt"

            with patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}, clear=False):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(outside),
                            "content": "should-not-write",
                        },
                    },
                    cwd=str(root),
                )

        self.assertIn("error", response)
        self.assertIn("HERMES_WRITE_SAFE_ROOT", str(response["error"]))
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()


# ── HOME env propagation tests (from PR #11285) ─────────────────────

from unittest.mock import patch as _patch
import pytest


def _make_home_client(tmp_path):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="copilot",
        acp_args=["--acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )


def _fake_popen_capture(captured):
    def _fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise FileNotFoundError("copilot not found")
    return _fake


def test_run_prompt_preserves_real_home_when_profile_home_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    captured = {}
    client = _make_home_client(tmp_path)

    with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
        with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
            client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["env"]["HOME"] == str(real_home)
    assert captured["kwargs"]["env"]["HERMES_REAL_HOME"] == str(real_home)


def test_run_prompt_passes_home_when_parent_env_is_clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    captured = {}
    client = _make_home_client(tmp_path)

    with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
        with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
            client._run_prompt("hello", timeout_seconds=1)

    assert "env" in captured["kwargs"]
    assert captured["kwargs"]["env"]["HOME"]


def test_match_session_model_id_prefers_exact_then_fuzzy():
    from agent.copilot_acp_client import _match_session_model_id

    available = [
        {"modelId": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
        {"modelId": "claude-opus-4-1", "name": "Claude Opus 4.1"},
    ]
    assert _match_session_model_id("claude-opus-4-1", available) == "claude-opus-4-1"
    assert _match_session_model_id("CLAUDE-OPUS-4-1", available) == "claude-opus-4-1"
    assert _match_session_model_id("opus", available) == "claude-opus-4-1"
    assert _match_session_model_id("sonnet 4.5", available) == "claude-sonnet-4-5"
    assert _match_session_model_id("gpt-5", available) is None
    assert _match_session_model_id("opus", "not-a-list") is None


# ── Session metadata capture + interactive steering (t_81c4183c) ──────
# End-to-end against a scripted fake ACP server: exercises the real
# _run_prompt transport, so it validates modes/configOptions/usage_update
# parsing and the multi-turn steer loop together.

import sys as _sys

_FAKE_ACP_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "sess-1",
            "modes": {"currentModeId": "default",
                      "availableModes": [{"id": "default", "name": "Default"}]},
            "configOptions": [
                {"id": "model", "name": "Model", "type": "select",
                 "currentValue": "claude-opus-4-8", "options": []},
                {"id": "effort", "name": "Effort", "type": "select",
                 "currentValue": "high", "options": []},
                {"id": "mode", "name": "Mode", "type": "select",
                 "currentValue": "default", "options": []},
            ]}})
    elif method == "session/prompt":
        text = msg["params"]["prompt"][0]["text"]
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess-1", "update": {
                "sessionUpdate": "usage_update", "used": 1500, "size": 200000,
                "cost": {"amount": 0.25, "currency": "USD"}}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess-1", "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "echo:" + text}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"totalTokens": 42, "inputTokens": 30, "outputTokens": 12}}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''


def _fake_acp_client(tmp_path):
    server = tmp_path / "fake_acp.py"
    server.write_text(_FAKE_ACP_SERVER)
    return CopilotACPClient(
        acp_command=_sys.executable,
        acp_args=[str(server)],
        acp_cwd=str(tmp_path),
    )


def test_run_prompt_captures_mode_effort_and_context_window(tmp_path):
    client = _fake_acp_client(tmp_path)
    text, _ = client._run_prompt("do the task", timeout_seconds=15)

    assert "echo:do the task" in text
    assert client.last_model == "claude-opus-4-8"
    assert client.last_mode == "default"
    assert client.last_effort == "high"
    assert client.last_context["context_used"] == 1500
    assert client.last_context["context_size"] == 200000
    assert client.last_context["context_remaining"] == 198500
    assert client.last_context["cost_usd"] == 0.25
    assert client.last_context["cost_currency"] == "USD"
    assert client.last_turn_usage["total_tokens"] == 42


def test_run_prompt_replays_operator_steer_on_same_session(tmp_path):
    client = _fake_acp_client(tmp_path)
    queued = iter(["please also add tests"])

    def follow_up():
        return next(queued, None)

    text, _ = client._run_prompt(
        "do the task", timeout_seconds=15, follow_up=follow_up,
    )

    # Both the primary turn and the injected steer turn ran on one session.
    assert "echo:do the task" in text
    assert "echo:please also add tests" in text
    assert "[operator steer]" in text
