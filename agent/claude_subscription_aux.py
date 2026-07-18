"""Auxiliary LLM client backed by the Claude Code subscription pool (ACP).

Lets auxiliary tasks (specify, decompose, compression, ...) fall back to the
same rotated Claude Code subscription pool the Kanban executor uses, so aux
functions keep working when every paid provider (OpenRouter, Codex OAuth,
Copilot, local model) is unavailable. Each ``create()`` runs a one-shot
haiku-tier ACP session on a leased subscription, respecting ``cooling_until``
and per-pocket concurrency via :func:`agent.claude_subscriptions.acquire`, and
logs ``token_usage`` with subscription attribution through the
``post_api_request`` plugin hook.

This is the aux-call analogue of
``agent.acp_task_executor._run_claude_code_session`` — stripped of the
workspace / steering / tool-feed / salvage concerns that only Kanban worker
sessions need. It deliberately reuses the same primitives (``command_for``,
``claude_subscriptions`` leasing, ``CopilotACPClient``) so behaviour stays in
lockstep with the main executor.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# haiku-tier by default: aux calls are cheap and latency-tolerant. "haiku"
# substring-matches the pool's advertised claude-haiku model id during the ACP
# session/set_model handshake (see copilot_acp_client._match_session_model_id),
# so we never pin a dated id that the vendor can retire.
DEFAULT_AUX_MODEL = "haiku"

# Bound rotations so a misbehaving pool can't spin forever. acquire() already
# raises NoSubscriptionAvailable when every pocket is cooling; this caps the
# number of distinct usage/auth-limit rotations chased within one create().
_MAX_ROTATIONS = 8


def _subscription_model(model: Optional[str]) -> str:
    """Coerce a resolved aux model to a Claude tier the pool can actually serve.

    The auxiliary resolver pre-fills ``model`` from the user's main model when a
    per-task ``auxiliary.<task>.model`` is absent — which may be an OpenRouter
    slug (``google/gemini-...``) or any non-Claude id the subscription pool
    cannot honour. Keep an explicit Claude tier (haiku/sonnet/opus) as-is;
    otherwise fall back to the cheap default.
    """
    text = (model or "").strip()
    if not text:
        return DEFAULT_AUX_MODEL
    if "/" in text:  # OpenRouter-format slug — not a native Claude model id
        return DEFAULT_AUX_MODEL
    lowered = text.lower()
    if any(tier in lowered for tier in ("haiku", "sonnet", "opus", "claude")):
        return text
    return DEFAULT_AUX_MODEL


def _aux_cwd() -> str:
    """A throwaway sandbox dir for the aux ACP session.

    Aux completions need no repository access — permissions are denied — and a
    stable scratch dir keeps the agent's cwd off any real project tree (and away
    from a project ``CLAUDE.md`` that would pollute a short spec-expansion call).
    """
    base = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "tmp" / "aux-acp"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return os.getcwd()
    return str(base)


class _Completions:
    def __init__(self, client: "ClaudeSubscriptionAuxClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create(**kwargs)


class _Chat:
    def __init__(self, client: "ClaudeSubscriptionAuxClient"):
        self.completions = _Completions(client)


class ClaudeSubscriptionAuxClient:
    """OpenAI-client-compatible facade over the Claude Code subscription pool.

    Exposes ``.chat.completions.create(**kwargs)`` returning the same
    ``SimpleNamespace`` completion shape as ``CopilotACPClient`` so it drops
    into ``call_llm`` unchanged. Stateless with respect to leases: each call
    acquires and releases its own subscription, so the instance is safe to
    cache and share across tasks.
    """

    def __init__(self, model: str, *, task: Optional[str] = None):
        self.model = model or DEFAULT_AUX_MODEL
        self._task = task or ""
        self.base_url = "acp://claude-subscriptions"
        self.api_key = "claude-subscriptions"
        self.chat = _Chat(self)

    def _create(self, **kwargs: Any) -> Any:
        from agent import claude_subscriptions as subs
        from agent.acp_task_executor import command_for
        from agent.copilot_acp_client import (
            ACPAuthError,
            ACPUsageLimitError,
            CopilotACPClient,
        )

        command, args = command_for("claude-code")
        cwd = _aux_cwd()
        model = _subscription_model(kwargs.pop("model", None) or self.model)
        rotations = 0
        while True:
            # This auxiliary client is Claude-only (it pins CLAUDE_CONFIG_DIR and
            # spawns the claude-code ACP), so it must never be handed a Codex
            # pocket from the vendor-agnostic pool.
            lease = subs.acquire(task_id=self._task or "aux",
                                 provider=subs.PROVIDER_CLAUDE)
            limited: Optional[str] = None
            client: Optional[Any] = None
            try:
                client = CopilotACPClient(
                    acp_command=command,
                    acp_args=args,
                    acp_cwd=cwd,
                    allow_permissions=False,
                    session_model=model,
                    extra_env={"CLAUDE_CONFIG_DIR": lease.config_dir},
                )
                completion = client._create_chat_completion(model=model, **kwargs)
                self._report_usage(client, lease.name)
                return completion
            except (ACPUsageLimitError, ACPAuthError) as exc:
                limited = str(exc)
            except Exception as exc:
                # Last-resort substring fallback over the full exception text for
                # limit/auth deaths the typed errors didn't wrap — mirrors the
                # Kanban executor so a verbose limit still rotates the pool.
                if subs.is_usage_limit_error(str(exc)) or subs.is_auth_error(str(exc)):
                    limited = str(exc)
                else:
                    raise
            finally:
                subs.release(lease)
            subs.mark_limited(lease.name, limited or "")
            rotations += 1
            if rotations >= _MAX_ROTATIONS:
                raise RuntimeError(
                    f"claude-subscriptions: exhausted {rotations} subscription "
                    f"rotations on usage/auth limits"
                )

    def _report_usage(self, client: Any, subscription: str) -> None:
        """Land aux-call usage in zeus.db ``token_usage`` with subscription tag.

        Best-effort: the zeus plugin's ``post_api_request`` hook performs the
        actual INSERT. A missing plugin, an agent that never advertised usage,
        or any hook error degrades silently — accounting must never break the
        aux call it is measuring.
        """
        usage = getattr(client, "last_turn_usage", None)
        if not usage:
            return
        context = getattr(client, "last_context", None) or {}
        try:
            from hermes_cli.plugins import discover_plugins, invoke_hook
            discover_plugins()
            invoke_hook(
                "post_api_request",
                task_id="",
                session_id=getattr(client, "last_session_id", "") or "",
                provider="acp-claude-code",
                api_mode="acp",
                model=getattr(client, "last_model", "") or self.model,
                usage=usage,
                subscription=subscription or "",
                effort=getattr(client, "last_effort", "") or "",
                context_used=context.get("context_used"),
                context_size=context.get("context_size"),
                cost_usd=context.get("cost_usd"),
            )
        except Exception:
            logger.debug("claude-subscriptions: usage report skipped", exc_info=True)


def build_client(
    model: Optional[str] = None,
    *,
    task: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(client, model)`` for the subscription pool, or ``(None, None)``.

    ``(None, None)`` when the pool is empty (no Claude Code logins on this host)
    so the auxiliary fallback chain continues to the next candidate instead of
    hard-failing. Cooling / at-capacity pockets are handled at ``create()`` time
    by :func:`agent.claude_subscriptions.acquire`, mirroring the Kanban
    executor's empty-pool gate.
    """
    try:
        from agent import claude_subscriptions as subs
        if subs.pool_size() == 0:
            return None, None
    except Exception:
        return None, None
    resolved = _subscription_model(model)
    return ClaudeSubscriptionAuxClient(resolved, task=task), resolved
