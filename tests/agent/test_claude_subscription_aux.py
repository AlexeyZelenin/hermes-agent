"""Unit tests for the Claude Code subscription auxiliary client.

Covers the aux-call bridge that lets specify/decompose/compression fall back
to the leased Claude Code subscription pool (ACP, haiku) when every paid
provider is down. The real pool leasing (``agent.claude_subscriptions``) and
the ACP subprocess (``CopilotACPClient``) are both mocked, so these tests never
spawn ``npx``, touch the OS keychain, or write the real zeus sidecar DB — they
verify the wiring: cwd/model/CLAUDE_CONFIG_DIR pinning, lease release, usage
attribution, model coercion, and usage-limit rotation.
"""

from types import SimpleNamespace

import pytest

from agent import auxiliary_client as ac
from agent import claude_subscription_aux as aux
from agent import claude_subscriptions as subs
from agent import copilot_acp_client as acp


class _FakeLease:
    def __init__(self, name, config_dir, provider="claude"):
        self.id = 1
        self.name = name
        self.config_dir = config_dir
        self.provider = provider


class _FakeACPClient:
    """Stand-in for CopilotACPClient capturing construction + one turn.

    ``behavior`` is a callable invoked by ``_create_chat_completion`` so a test
    can return a completion or raise a limit/auth error to drive rotation.
    """

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.last_turn_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.last_context = {"context_used": 15}
        self.last_session_id = "sess-1"
        self.last_model = kwargs.get("session_model") or "haiku"
        self.last_effort = ""
        _FakeACPClient.instances.append(self)

    def _create_chat_completion(self, **kwargs):
        return _FakeACPClient._behavior(self, **kwargs)


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeACPClient.instances = []
    yield


@pytest.fixture
def patched(monkeypatch):
    """Wire the aux client onto fake pool + fake ACP subprocess."""
    released = []
    limited = []
    acquired = []

    leases = [_FakeLease("personal", "/tmp/cfg-personal")]

    def _acquire(task_id="", provider=None):
        acquired.append(task_id)
        return leases[0]

    monkeypatch.setattr(subs, "acquire", _acquire)
    monkeypatch.setattr(subs, "release", lambda lease: released.append(lease.name))
    monkeypatch.setattr(subs, "mark_limited", lambda name, msg, **k: limited.append((name, msg)))
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: len(leases))
    monkeypatch.setattr(acp, "CopilotACPClient", _FakeACPClient)
    # command_for is real; keep it, but make it env-independent for the test.
    monkeypatch.setattr(
        "agent.acp_task_executor.command_for",
        lambda executor: ("npx", ["--yes", "@agentclientprotocol/claude-agent-acp"]),
    )
    return {"released": released, "limited": limited, "acquired": acquired, "leases": leases}


# ── model coercion ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given,expected",
    [
        ("", "haiku"),
        (None, "haiku"),
        ("google/gemini-3-flash", "haiku"),   # OpenRouter slug -> default
        ("gpt-5.5", "haiku"),                  # non-Claude id -> default
        ("sonnet", "sonnet"),                  # explicit Claude tier kept
        ("opus", "opus"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ],
)
def test_subscription_model_coercion(given, expected):
    assert aux._subscription_model(given) == expected


# ── build_client gate ─────────────────────────────────────────────────────


def test_build_client_empty_pool_returns_none(monkeypatch):
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 0)
    client, model = aux.build_client("sonnet")
    assert client is None and model is None


def test_build_client_nonempty_pool_returns_client(monkeypatch):
    monkeypatch.setattr(subs, "pool_size", lambda provider=None: 3)
    client, model = aux.build_client("google/x")
    assert isinstance(client, aux.ClaudeSubscriptionAuxClient)
    assert model == "haiku"  # slug coerced to default tier


# ── happy path ────────────────────────────────────────────────────────────


def test_create_pins_config_dir_and_releases(patched):
    def _ok(self, **kwargs):
        return {"choices": [{"message": {"content": "{}"}}]}

    _FakeACPClient._behavior = _ok
    client = aux.ClaudeSubscriptionAuxClient("haiku", task="triage_specifier")
    result = client.chat.completions.create(
        model="haiku",
        messages=[{"role": "user", "content": "hi"}],
        timeout=30,
    )
    assert result == {"choices": [{"message": {"content": "{}"}}]}
    # exactly one lease, pinned to the leased dir, released after
    assert len(_FakeACPClient.instances) == 1
    built = _FakeACPClient.instances[0]
    assert built.kwargs["extra_env"] == {"CLAUDE_CONFIG_DIR": "/tmp/cfg-personal"}
    assert built.kwargs["session_model"] == "haiku"
    assert built.kwargs["allow_permissions"] is False
    assert patched["released"] == ["personal"]
    assert patched["limited"] == []


def test_create_reports_usage_with_subscription(patched, monkeypatch):
    hook_calls = []
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kw: hook_calls.append((name, kw)),
    )
    _FakeACPClient._behavior = lambda self, **kw: {"ok": True}
    client = aux.ClaudeSubscriptionAuxClient("haiku")
    client.chat.completions.create(messages=[{"role": "user", "content": "x"}])
    assert len(hook_calls) == 1
    name, kw = hook_calls[0]
    assert name == "post_api_request"
    assert kw["subscription"] == "personal"
    assert kw["provider"] == "acp-claude-code"
    assert kw["usage"]["total_tokens"] == 15


# ── rotation on usage limit ────────────────────────────────────────────────


def test_create_rotates_on_usage_limit(patched, monkeypatch):
    # First lease raises a usage-limit death, second succeeds.
    patched["leases"].insert(0, _FakeLease("work", "/tmp/cfg-work"))
    seq = iter(patched["leases"])
    monkeypatch.setattr(subs, "acquire", lambda task_id="", provider=None: next(seq))

    calls = {"n": 0}

    def _behavior(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise acp.ACPUsageLimitError("usage limit reached")
        return {"ok": "second"}

    _FakeACPClient._behavior = _behavior
    client = aux.ClaudeSubscriptionAuxClient("haiku")
    result = client.chat.completions.create(messages=[{"role": "user", "content": "x"}])
    assert result == {"ok": "second"}
    # first pocket cooled, both leases released
    assert patched["limited"] == [("work", "usage limit reached")]
    assert patched["released"] == ["work", "personal"]


def test_create_reraises_non_limit_error(patched):
    def _boom(self, **kwargs):
        raise ValueError("bad request, not a limit")

    _FakeACPClient._behavior = _boom
    client = aux.ClaudeSubscriptionAuxClient("haiku")
    with pytest.raises(ValueError):
        client.chat.completions.create(messages=[{"role": "user", "content": "x"}])
    # lease still released, pocket NOT cooled (structural error, not a limit)
    assert patched["released"] == ["personal"]
    assert patched["limited"] == []


# ── last-resort gating in auxiliary_client ─────────────────────────────────


class TestSubscriptionLastResort:
    def test_default_allowlist(self, monkeypatch):
        # With no config override, the default critical set is returned.
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"auxiliary": {}}
        )
        assert ac._subscription_last_resort_tasks() == {
            "triage_specifier",
            "kanban_decomposer",
            "compression",
        }

    def test_config_override(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"auxiliary": {"subscription_last_resort": ["compression", " x "]}},
        )
        assert ac._subscription_last_resort_tasks() == {"compression", "x"}

    def test_empty_list_disables(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"auxiliary": {"subscription_last_resort": []}},
        )
        client, model = ac._build_subscription_last_resort_client(
            "compression", "openrouter"
        )
        assert client is None and model is None

    def test_task_not_in_allowlist_skips(self, monkeypatch):
        monkeypatch.setattr(subs, "pool_size", lambda provider=None: 3)
        client, model = ac._build_subscription_last_resort_client(
            "title_generation", "openrouter"
        )
        assert client is None and model is None

    def test_no_self_recursion(self, monkeypatch):
        monkeypatch.setattr(subs, "pool_size", lambda provider=None: 3)
        # Failed provider already IS the subscription pool — must not recurse.
        client, model = ac._build_subscription_last_resort_client(
            "compression", "claude-subscriptions"
        )
        assert client is None and model is None

    def test_builds_when_enabled_and_pool_present(self, monkeypatch):
        monkeypatch.setattr(subs, "pool_size", lambda provider=None: 2)
        client, model = ac._build_subscription_last_resort_client(
            "compression", "openrouter"
        )
        assert isinstance(client, aux.ClaudeSubscriptionAuxClient)
        assert model == "haiku"

    def test_no_op_when_pool_empty(self, monkeypatch):
        monkeypatch.setattr(subs, "pool_size", lambda provider=None: 0)
        client, model = ac._build_subscription_last_resort_client(
            "compression", "openrouter"
        )
        assert client is None and model is None


def test_call_llm_falls_back_to_subscription_on_payment_error(monkeypatch):
    """The reported outage: paid provider 402s, no other fallback, subscriptions serve."""
    from agent.auxiliary_client import call_llm

    primary = _make_402_client()
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{\"title\": \"ok\"}"))]
    )
    sub_client = _FixedClient(resp)

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda *a, **k: ("auto", "google/x", None, None, None),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        lambda *a, **k: (primary, "google/x"),
    )
    # Every configured/paid/free fallback returns nothing — the outage.
    monkeypatch.setattr(
        "agent.auxiliary_client._try_configured_fallback_chain",
        lambda *a, **k: (None, None, ""),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._try_main_fallback_chain",
        lambda *a, **k: (None, None, ""),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._try_payment_fallback",
        lambda *a, **k: (None, None, ""),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._build_subscription_last_resort_client",
        lambda task, failed: (sub_client, "haiku"),
    )

    out = call_llm(task="triage_specifier", messages=[{"role": "user", "content": "x"}])
    assert out is resp
    assert sub_client.called


class _FixedClient:
    def __init__(self, resp):
        self._resp = resp
        self.called = False
        self.base_url = "acp://claude-subscriptions"
        self.api_key = "claude-subscriptions"
        chat = SimpleNamespace()
        chat.completions = SimpleNamespace(create=self._create)
        self.chat = chat

    def _create(self, **kwargs):
        self.called = True
        return self._resp


class _Err402(Exception):
    def __init__(self):
        super().__init__("402 payment required")
        self.status_code = 402


def _make_402_client():
    client = SimpleNamespace()
    client.base_url = "https://openrouter.ai/api/v1"
    chat = SimpleNamespace()

    def _create(**kwargs):
        raise _Err402()

    chat.completions = SimpleNamespace(create=_create)
    client.chat = chat
    return client
