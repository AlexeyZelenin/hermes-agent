"""The ACP executor must merge plugin-contributed worker env (the Langfuse OTLP
env from Zeus's ``contribute_worker_env`` hook) into the spawning client's
``extra_env`` - the integration seam that makes Claude Code stream per-request /
per-tool spans natively. Written after review found the ``session_otel_env``
contract green on unit level yet dead on integration: nothing called it (see
task t_795601eb). These tests pin the wiring so the gap can't silently reopen.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


# A representative slice of langfuse_config.session_otel_env()'s output. The core
# test deliberately does NOT import the Zeus plugin - it asserts the executor
# forwards whatever the hook returns, using the real contract's key names so the
# test doubles as documentation of what must reach the worker.
FAKE_OTEL_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://lf.example/api/public/otel",
    "OTEL_LOG_TOOL_CONTENT": "1",
    "OTEL_RESOURCE_ATTRIBUTES": "service.name=zeus-worker,zeus.task_id=t_x",
}


@pytest.fixture
def kanban_conn(tmp_path):
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


class _connection_context:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


class _FakeLease:
    def __init__(self, name, provider="claude"):
        self.id = 1
        self.name = name
        self.config_dir = f"/cfg/{name}"
        self.provider = provider


def _hook_stub(returns):
    """A plugins.invoke_hook replacement that answers only contribute_worker_env
    (with ``returns``) and stays empty for every other hook the run fires."""
    def stub(name, **kw):
        if name == "contribute_worker_env":
            stub.calls.append(kw)
            return list(returns)
        return []
    stub.calls = []
    return stub


def _patch_plugins(monkeypatch, hook):
    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "discover_plugins", lambda force=False: None)
    monkeypatch.setattr(plugins, "invoke_hook", hook)


def test_contributed_env_merges_into_pool_worker_extra_env(monkeypatch, kanban_conn, tmp_path):
    """Pool path: the OTLP env from contribute_worker_env lands in the spawning
    client's extra_env, alongside (and never clobbering) CLAUDE_CONFIG_DIR."""
    from agent import acp_task_executor as executor
    from agent import claude_subscriptions as subs

    task_id = kb.create_task(kanban_conn, title="OTel task", assignee="external")
    assert kb.claim_task(kanban_conn, task_id, claimer="test-lock") is not None
    monkeypatch.setattr(kb, "connect_closing", lambda *, board=None: _connection_context(kanban_conn))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 1)
    monkeypatch.setattr(subs, "acquire", lambda task_id="", provider=None: _FakeLease("p1"))
    monkeypatch.setattr(subs, "release", lambda lease: None)

    hook = _hook_stub([FAKE_OTEL_ENV])
    _patch_plugins(monkeypatch, hook)

    captured = {}

    class FakeClient:
        last_model = "claude-opus-4-8"
        last_turn_usage = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.last_partial_text = ""

        def _run_prompt(self, prompt, *, timeout_seconds, follow_up=None):
            return "done", ""

    monkeypatch.setattr(executor, "CopilotACPClient", FakeClient)
    monkeypatch.setattr(executor, "command_for", lambda name: ("fake-acp", ["--stdio"]))

    executor.run_task(executor="claude-code", task_id=task_id, workspace=str(tmp_path), board="test")

    env = captured["extra_env"]
    for key, value in FAKE_OTEL_ENV.items():
        assert env[key] == value, f"{key} must be forwarded to the worker"
    assert env["CLAUDE_CONFIG_DIR"] == "/cfg/p1"
    # The hook is addressed with the task binding used for the OTel resource attrs.
    assert hook.calls[0]["task_id"] == task_id
    assert hook.calls[0]["subscription"] == "p1"


def test_core_config_dir_wins_over_contributed_collision(monkeypatch, kanban_conn, tmp_path):
    """A plugin that (mis)returns CLAUDE_CONFIG_DIR must never override the leased
    subscription dir - the core key is applied last on purpose."""
    from agent import acp_task_executor as executor
    from agent import claude_subscriptions as subs

    task_id = kb.create_task(kanban_conn, title="Collision", assignee="external")
    assert kb.claim_task(kanban_conn, task_id, claimer="test-lock") is not None
    monkeypatch.setattr(kb, "connect_closing", lambda *, board=None: _connection_context(kanban_conn))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 1)
    monkeypatch.setattr(subs, "acquire", lambda task_id="", provider=None: _FakeLease("p9"))
    monkeypatch.setattr(subs, "release", lambda lease: None)
    _patch_plugins(monkeypatch, _hook_stub([{"CLAUDE_CONFIG_DIR": "/evil", "OTEL_TRACES_EXPORTER": "otlp"}]))

    captured = {}

    class FakeClient:
        last_model = "claude-opus-4-8"
        last_turn_usage = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.last_partial_text = ""

        def _run_prompt(self, prompt, *, timeout_seconds, follow_up=None):
            return "done", ""

    monkeypatch.setattr(executor, "CopilotACPClient", FakeClient)
    monkeypatch.setattr(executor, "command_for", lambda name: ("fake-acp", ["--stdio"]))

    executor.run_task(executor="claude-code", task_id=task_id, workspace=str(tmp_path), board="test")
    assert captured["extra_env"]["CLAUDE_CONFIG_DIR"] == "/cfg/p9"
    assert captured["extra_env"]["OTEL_TRACES_EXPORTER"] == "otlp"


def test_contributed_env_in_pool0_fallback_path(monkeypatch, kanban_conn, tmp_path):
    """Empty-pool fallback (no login dirs): the contributed OTLP env still reaches
    the single-session client's extra_env."""
    from agent import acp_task_executor as executor
    from agent import claude_subscriptions as subs

    task_id = kb.create_task(kanban_conn, title="Pool0 OTel", assignee="external")
    assert kb.claim_task(kanban_conn, task_id, claimer="test-lock") is not None
    monkeypatch.setattr(kb, "connect_closing", lambda *, board=None: _connection_context(kanban_conn))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 0)
    _patch_plugins(monkeypatch, _hook_stub([FAKE_OTEL_ENV]))

    captured = {}

    class FakeClient:
        last_model = "claude-opus-4-8"
        last_turn_usage = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.last_partial_text = ""

        def _run_prompt(self, prompt, *, timeout_seconds, follow_up=None):
            return "done", ""

    monkeypatch.setattr(executor, "CopilotACPClient", FakeClient)
    monkeypatch.setattr(executor, "command_for", lambda name: ("fake-acp", ["--stdio"]))

    executor.run_task(executor="claude-code", task_id=task_id, workspace=str(tmp_path), board="test")
    assert captured["extra_env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_fail_open_when_no_plugin_contributes(monkeypatch, kanban_conn, tmp_path):
    """Unconfigured Langfuse (hook returns nothing): the pool worker keeps exactly
    its CLAUDE_CONFIG_DIR and no OTel keys - ordinary tasks are unaffected."""
    from agent import acp_task_executor as executor
    from agent import claude_subscriptions as subs

    task_id = kb.create_task(kanban_conn, title="Plain", assignee="external")
    assert kb.claim_task(kanban_conn, task_id, claimer="test-lock") is not None
    monkeypatch.setattr(kb, "connect_closing", lambda *, board=None: _connection_context(kanban_conn))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 1)
    monkeypatch.setattr(subs, "acquire", lambda task_id="", provider=None: _FakeLease("p1"))
    monkeypatch.setattr(subs, "release", lambda lease: None)
    _patch_plugins(monkeypatch, _hook_stub([]))

    captured = {}

    class FakeClient:
        last_model = "claude-opus-4-8"
        last_turn_usage = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.last_partial_text = ""

        def _run_prompt(self, prompt, *, timeout_seconds, follow_up=None):
            return "done", ""

    monkeypatch.setattr(executor, "CopilotACPClient", FakeClient)
    monkeypatch.setattr(executor, "command_for", lambda name: ("fake-acp", ["--stdio"]))

    executor.run_task(executor="claude-code", task_id=task_id, workspace=str(tmp_path), board="test")
    assert captured["extra_env"] == {"CLAUDE_CONFIG_DIR": "/cfg/p1"}


def test_hook_error_is_swallowed_and_spawn_proceeds(monkeypatch, kanban_conn, tmp_path):
    """A raising discovery/hook path must not block spawn: extra_env falls back to
    just CLAUDE_CONFIG_DIR and the task still completes (fail-open)."""
    from agent import acp_task_executor as executor
    from agent import claude_subscriptions as subs
    import hermes_cli.plugins as plugins

    task_id = kb.create_task(kanban_conn, title="Boom", assignee="external")
    assert kb.claim_task(kanban_conn, task_id, claimer="test-lock") is not None
    monkeypatch.setattr(kb, "connect_closing", lambda *, board=None: _connection_context(kanban_conn))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 1)
    monkeypatch.setattr(subs, "acquire", lambda task_id="", provider=None: _FakeLease("p1"))
    monkeypatch.setattr(subs, "release", lambda lease: None)

    def boom(force=False):
        raise RuntimeError("plugin discovery blew up")

    monkeypatch.setattr(plugins, "discover_plugins", boom)

    captured = {}

    class FakeClient:
        last_model = "claude-opus-4-8"
        last_turn_usage = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.last_partial_text = ""

        def _run_prompt(self, prompt, *, timeout_seconds, follow_up=None):
            return "done", ""

    monkeypatch.setattr(executor, "CopilotACPClient", FakeClient)
    monkeypatch.setattr(executor, "command_for", lambda name: ("fake-acp", ["--stdio"]))

    result = executor.run_task(executor="claude-code", task_id=task_id, workspace=str(tmp_path), board="test")
    assert result == "done"
    assert captured["extra_env"] == {"CLAUDE_CONFIG_DIR": "/cfg/p1"}
