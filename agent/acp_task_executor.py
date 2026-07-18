"""One-session external ACP executor for Kanban tasks; separate from providers."""
from __future__ import annotations
import json, os, shlex
from agent.copilot_acp_client import CopilotACPClient

_DEFAULT = {"claude-code": ("npx", ["--yes", "@agentclientprotocol/claude-agent-acp"]), "codex": ("codex-acp", ["--stdio"])}
def command_for(executor):
    if executor not in _DEFAULT: raise ValueError(f"unsupported ACP executor: {executor}")
    prefix = "HERMES_CLAUDE_CODE_ACP" if executor == "claude-code" else "HERMES_CODEX_ACP"
    command = os.getenv(prefix + "_COMMAND", "").strip() or _DEFAULT[executor][0]
    raw = os.getenv(prefix + "_ARGS", "").strip()
    return command, shlex.split(raw) if raw else list(_DEFAULT[executor][1])
def _is_acp_auth_failure(exc):
    """True when ``exc`` is an ACP authentication failure (e.g. Copilot 403) as
    opposed to a transient quota wall or a task-specific error. Only auth
    failures blacklist the provider (task t_08676525)."""
    try:
        from agent.copilot_acp_client import ACPAuthError
        if isinstance(exc, ACPAuthError):
            return True
    except Exception:
        pass
    try:
        from agent import claude_subscriptions as subs
        return bool(subs.is_auth_error(str(exc)))
    except Exception:
        return False
def steer_inbox_path(task_id, board=None):
    """Per-task interactive-steering control channel: a JSONL inbox under the
    board dir that the Zeus dashboard appends operator messages to and the live
    ACP session drains between turns. One file per task keeps boards isolated."""
    from hermes_cli import kanban_db as kb
    return kb.board_dir(board) / "steer" / f"{task_id}.jsonl"
def enqueue_steer(task_id, text, board=None):
    """Append one operator steering message to the task's control channel."""
    path = steer_inbox_path(task_id, board)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"text": str(text)}, ensure_ascii=False) + "\n")
def _drain_steer(task_id, board=None):
    """Return all queued steer messages joined into one turn (or None when the
    inbox is empty), moving consumed lines to a ``.done`` sibling so a message
    is never silently lost if the turn then fails. Best-effort file I/O."""
    path = steer_inbox_path(task_id, board)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None
    texts = []
    for ln in lines:
        try:
            texts.append(str(json.loads(ln).get("text") or ""))
        except (ValueError, TypeError):
            continue
    try:
        with open(path.with_suffix(".done.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(raw if raw.endswith("\n") else raw + "\n")
        path.write_text("", encoding="utf-8")
    except OSError:
        pass
    joined = "\n\n".join(t for t in texts if t.strip())
    return joined or None
def _steering_enabled():
    """Interactive steering is on unless explicitly disabled. An empty inbox is
    a no-op (one file stat per task), so the default is safe."""
    return os.getenv("HERMES_ACP_STEERING", "1").strip().lower() not in ("0", "false", "no", "off")
def _stamp_session_metadata(metadata, client):
    """Layer the adapter's per-session facts (permission mode, reasoning effort,
    context-window occupancy, cumulative cost) onto the run metadata so the
    card's 'last run' block can show them. Only keys the adapter provided."""
    mode = getattr(client, "last_mode", "") or ""
    if mode: metadata["mode"] = mode
    effort = getattr(client, "last_effort", "") or ""
    if effort: metadata["effort"] = effort
    context = getattr(client, "last_context", None) or {}
    for key in ("context_used", "context_size", "context_remaining", "cost_usd", "cost_currency"):
        if context.get(key) is not None:
            metadata[key] = context[key]
def _report_usage(client, executor, task_id, subscription=None):
    """Best-effort turn accounting: the post_api_request hook lands usage in zeus.db token_usage."""
    usage = getattr(client, "last_turn_usage", None)
    if not usage: return
    context = getattr(client, "last_context", None) or {}
    try:
        from hermes_cli.plugins import discover_plugins, invoke_hook
        discover_plugins()
        invoke_hook("post_api_request", task_id=task_id,
                    session_id=getattr(client, "last_session_id", "") or "",
                    provider=f"acp-{executor}", api_mode="acp",
                    model=getattr(client, "last_model", "") or executor, usage=usage,
                    subscription=subscription or "",
                    effort=getattr(client, "last_effort", "") or "",
                    context_used=context.get("context_used"),
                    context_size=context.get("context_size"),
                    cost_usd=context.get("cost_usd"))
    except Exception:
        pass
def _contributed_worker_env(task_id, board=None, subscription=None, run_id=None):
    """Plugin-contributed env for a spawning ACP worker, merged before spawn.

    Fires the ``contribute_worker_env`` hook so a plugin can inject env into the
    worker session WITHOUT the core importing it - e.g. the Zeus plugin resolves
    ``HERMES_LANGFUSE_*`` into the OTLP env that makes Claude Code stream
    per-request/per-tool spans to Langfuse natively. Each callback returns a
    ``dict[str, str]`` (or ``None``); later callbacks win on a key collision.
    Fail-open: invoke_hook isolates every callback, and any error reaching here
    yields ``{}`` so an unconfigured or broken plugin never blocks task spawn."""
    try:
        from hermes_cli.plugins import discover_plugins, invoke_hook
        discover_plugins()
        results = invoke_hook("contribute_worker_env", task_id=task_id,
                              board=board, subscription=subscription, run_id=run_id)
    except Exception:
        return {}
    merged = {}
    for result in results:
        if isinstance(result, dict):
            merged.update({str(k): str(v) for k, v in result.items()})
    return merged
def _new_client(command, args, workspace, model, extra_env=None, effort=None,
                tool_activity_sink=None):
    return CopilotACPClient(acp_command=command, acp_args=args, acp_cwd=workspace,
                            allow_permissions=True, session_model=model,
                            session_effort=effort, extra_env=extra_env,
                            tool_activity_sink=tool_activity_sink)
def _tool_feed_enabled():
    """Live tool-call feed is on unless explicitly disabled. It only writes on a
    tool's first sighting and status transitions, so volume is bounded; the flag
    exists so a noisy board can opt out without a code change."""
    return os.getenv("HERMES_ACP_TOOL_FEED", "1").strip().lower() not in ("0", "false", "no", "off")
def _tool_input_preview(raw_input, limit=200):
    """One-line, length-capped preview of a tool call's raw input for the feed.
    Prefers the human-meaningful field (a subagent spawn's description/prompt, a
    command, a path) and falls back to compact JSON."""
    if raw_input is None:
        return ""
    text = ""
    if isinstance(raw_input, dict):
        for key in ("description", "prompt", "command", "query", "pattern", "path", "url"):
            val = raw_input.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
        else:
            try:
                text = json.dumps(raw_input, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(raw_input)
    else:
        text = str(raw_input)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:max(limit - 3, 0)].rstrip() + "..."
    return text
def _tool_event_payload(event):
    """Compact task_events payload for one tool-call transition. Keeps only the
    live-feed essentials (id, status, title, kind, touched paths, input hint) -
    full per-tool history stays in OTel, so this stays small on the wire."""
    payload = {"tool_call_id": event.get("tool_call_id"),
               "status": event.get("status") or "pending"}
    for key in ("title", "kind"):
        if event.get(key):
            payload[key] = event[key]
    paths = [l.get("path") for l in (event.get("locations") or [])
             if isinstance(l, dict) and l.get("path")]
    if paths:
        payload["locations"] = paths[:5]
    preview = _tool_input_preview(event.get("raw_input"))
    if preview:
        payload["input"] = preview
    return payload
def _make_tool_activity_sink(task_id, board, run_id):
    """Return a CopilotACPClient tool_activity_sink that mirrors ACP tool calls
    onto the task's live event feed. Fires only on a tool's first sighting and
    each status transition (running -> completed/failed), so the card shows a
    clean 'spawn -> running -> done' line per tool rather than every streamed
    content chunk. Best-effort: a DB failure never disturbs the ACP session."""
    from hermes_cli import kanban_db as kb
    def _sink(event):
        if not event.get("is_new") and not event.get("status_changed"):
            return
        try:
            kb.append_task_event(task_id, "tool_call", _tool_event_payload(event),
                                 run_id=run_id, board=board)
        except Exception:
            pass
    return _sink
def _salvage_partial_output(task_id, board, client):
    """Persist a limit/auth-interrupted session's partial output as a task
    comment so the next attempt resumes with context instead of blind.

    The streamed agent text is otherwise discarded when the pool rotates to
    the next subscription (the dying session's ``client`` is dropped). Only
    real streamed work is salvaged - an empty or whitespace buffer is a no-op.
    Best-effort: a persistence failure must never mask the underlying limit."""
    try:
        partial = (getattr(client, "last_partial_text", "") or "").strip()
    except Exception:
        partial = ""
    if not partial:
        return
    try:
        from hermes_cli import kanban_db as kb
        with kb.connect_closing(board=board) as conn:
            kb.add_comment(conn, task_id, author="claude-code",
                           body="partial handoff (limit-interrupted)\n\n" + partial)
    except Exception:
        pass
def _run_claude_code_session(command, args, workspace, prompt, timeout, model, task_id, follow_up=None, board=None, effort=None, tool_activity_sink=None, run_id=None):
    """Run the prompt on the Claude subscription pool, rotating on usage limits.

    Each session gets CLAUDE_CONFIG_DIR pinned to a leased subscription dir
    ('spread' strategy). A session that dies on a usage limit puts that
    subscription into cooldown and the task retries immediately on the next
    one; NoSubscriptionAvailable propagates when the whole pool is cooling.
    An empty pool (no login dirs on this host) falls back to the legacy
    single-session path with Claude Code's own default config dir.
    """
    from agent import claude_subscriptions as subs
    if subs.pool_size() == 0:
        extra_env = _contributed_worker_env(task_id, board, None, run_id) or None
        client = _new_client(command, args, workspace, model, extra_env=extra_env,
                             effort=effort, tool_activity_sink=tool_activity_sink)
        text, _ = client._run_prompt(prompt, timeout_seconds=timeout, follow_up=follow_up)
        return text, client, None
    while True:
        lease = subs.acquire(task_id=task_id)
        limited = None
        client = None
        try:
            # Plugin-contributed env (e.g. Zeus Langfuse OTLP) first, then the
            # core-owned CLAUDE_CONFIG_DIR last so the leased subscription dir
            # always wins over any contributed key.
            extra_env = _contributed_worker_env(task_id, board, lease.name, run_id)
            extra_env["CLAUDE_CONFIG_DIR"] = lease.config_dir
            client = _new_client(command, args, workspace, model,
                                 extra_env=extra_env,
                                 effort=effort, tool_activity_sink=tool_activity_sink)
            text, _ = client._run_prompt(prompt, timeout_seconds=timeout, follow_up=follow_up)
            # A completed session/prompt is structurally NOT a limit/auth death:
            # the request succeeded and this is the task's handoff. Substring-
            # scanning it (e.g. a report that merely mentions "usage limit
            # reached on 429") is what discarded committed work and looped the
            # task. Genuine limit/auth deaths abort the request and arrive as the
            # typed exceptions handled below, so the returned text is trusted.
            return text, client, lease.name
        except Exception as exc:
            # Structural signal first (typed error from the ACP client), then a
            # last-resort substring fallback over the FULL exception text - no
            # length cap, so a verbose limit/auth exception still rotates instead
            # of parking the task while the pool is free.
            from agent.copilot_acp_client import ACPUsageLimitError, ACPAuthError
            if not isinstance(exc, (ACPUsageLimitError, ACPAuthError)) \
                    and not subs.is_usage_limit_error(str(exc)) \
                    and not subs.is_auth_error(str(exc)):
                raise
            limited = str(exc)
        finally:
            subs.release(lease)
        _salvage_partial_output(task_id, board, client)
        subs.mark_limited(lease.name, limited)
def run_task(*, executor, task_id, workspace, board=None):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(board=board) as conn:
        task=kb.get_task(conn, task_id)
        if not task: raise ValueError(f"unknown task {task_id}")
        context, run_id = kb.build_worker_context(conn, task_id), task.current_run_id
    command,args=command_for(executor)
    prompt=("You are the sole native external coding-harness session for this already-scoped task. Work only in the supplied cwd; do not orchestrate child tasks. Follow project rules and return a concise factual handoff with tests run. Never create test fixtures against live shared or host state: tests that need a kanban board must spin up an isolated one (set HERMES_KANBAN_HOME to a temp dir); tests that touch the OS keychain, credential stores, or other host state must use a temporary/throwaway store (e.g. a temp keychain via `security create-keychain`) or mock the calls - NEVER the real login keychain or live data, which prompts the user and pollutes their system. Clean up in teardown. If the cwd is a git repository and you changed files: run the relevant tests and COMMIT your work (conventional-commits message referencing the task id) before finishing - completing a code task with a dirty tree is a protocol violation; do not push.\n\n"+context)
    # Project-frozen append-system-prompt (HERMES_KANBAN_APPEND_PROMPT): the
    # ACP-path stand-in for a manual `claude --append-system-prompt`. This ACP
    # server takes no such CLI flag, so the guidance rides at the head of the
    # worker prompt, framed as project context. Empty/unset -> unchanged prompt.
    append_prompt=os.getenv("HERMES_KANBAN_APPEND_PROMPT","").strip()
    if append_prompt:
        prompt="[project context]\n"+append_prompt+"\n\n"+prompt
    try:
        timeout=float(os.getenv("HERMES_ACP_TIMEOUT_SECONDS", "3600"))
        model=os.getenv("HERMES_KANBAN_MODEL","").strip() or None
        effort=os.getenv("HERMES_KANBAN_EFFORT","").strip() or None
        subscription=None
        follow_up=(lambda: _drain_steer(task_id, board)) if _steering_enabled() else None
        tool_sink=_make_tool_activity_sink(task_id, board, run_id) if _tool_feed_enabled() else None
        if executor == "claude-code":
            text,client,subscription=_run_claude_code_session(command,args,workspace,prompt,timeout,model,task_id,follow_up,board,effort,tool_sink,run_id)
        else:
            client=_new_client(command,args,workspace,model,effort=effort,tool_activity_sink=tool_sink)
            text,_=client._run_prompt(prompt,timeout_seconds=timeout,follow_up=follow_up)
        _report_usage(client,executor,task_id,subscription)
    except Exception as exc:
        with kb.connect_closing(board=board) as conn:
            kb.block_task(conn,task_id,reason=f"External {executor} ACP session failed: {exc}",kind="capability",expected_run_id=run_id)
            # Auth failure (e.g. Copilot 403) is a PROVIDER fault, not a task
            # fault: blacklist the ACP channel so the dispatcher's preflight
            # gate (check_respawn_guard) stops handing fresh sessions to it, and
            # surface it in "Проблемы" for the operator (task t_08676525).
            if _is_acp_auth_failure(exc):
                provider=f"acp-{executor}"
                try:
                    from hermes_cli import provider_health
                    provider_health.record_unavailable(conn,provider,reason=f"ACP auth failed: {exc}"[:200])
                    provider_health.emit_health_finding(provider,status="unhealthy",reason=f"сбой auth ACP (task {task_id}): {exc}"[:200])
                except Exception:
                    pass
        raise
    metadata={"executor":executor,"acp_command":command,"provider":f"acp-{executor}","workspace":workspace}
    run_model=getattr(client,"last_model","") or model
    if run_model: metadata["model"]=run_model
    if effort: metadata["effort_requested"]=effort
    if subscription: metadata["subscription"]=subscription
    _stamp_session_metadata(metadata, client)
    with kb.connect_closing(board=board) as conn:
        if not kb.complete_task(conn,task_id,summary=text.strip() or f"External {executor} ACP session completed.",metadata=metadata,expected_run_id=run_id): raise RuntimeError("task was reclaimed or terminal")
    return text
def main():
    return run_task(executor=os.environ["HERMES_KANBAN_EXECUTOR"],task_id=os.environ["HERMES_KANBAN_TASK"],workspace=os.environ["HERMES_KANBAN_WORKSPACE"],board=os.getenv("HERMES_KANBAN_BOARD")) and 0
if __name__ == "__main__": main()
