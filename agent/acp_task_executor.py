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
def _new_client(command, args, workspace, model, extra_env=None, effort=None):
    return CopilotACPClient(acp_command=command, acp_args=args, acp_cwd=workspace,
                            allow_permissions=True, session_model=model,
                            session_effort=effort, extra_env=extra_env)
def _tool_activity_writer(task_id, board, run_id, client):
    """Build the ACP ``on_tool_activity`` callback that streams the live feed.

    Fires on each tool-call transition (new call / status change): writes the
    aggregated per-run snapshot into ``task_runs.metadata`` and appends a
    compact ``tool_call`` event so the dashboard card/drawer update live -
    sub-agent spawns show up as a row that flips running -> completed. Returns
    ``None`` (no callback) when there is no run to attach activity to.
    Best-effort: a DB hiccup must never break the running session."""
    if not run_id:
        return None
    def _on_activity(changed):
        try:
            snapshot = client.tool_activity_snapshot()
        except Exception:
            return
        try:
            from hermes_cli import kanban_db as kb
            with kb.connect_closing(board=board) as conn:
                kb.record_run_tool_activity(conn, task_id, run_id, snapshot, changed)
        except Exception:
            pass
    return _on_activity
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
def _run_claude_code_session(command, args, workspace, prompt, timeout, model, task_id, follow_up=None, board=None, effort=None, run_id=None):
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
        client = _new_client(command, args, workspace, model, effort=effort)
        text, _ = client._run_prompt(prompt, timeout_seconds=timeout, follow_up=follow_up,
                                     on_tool_activity=_tool_activity_writer(task_id, board, run_id, client))
        return text, client, None
    while True:
        lease = subs.acquire(task_id=task_id)
        limited = None
        client = None
        try:
            client = _new_client(command, args, workspace, model,
                                 extra_env={"CLAUDE_CONFIG_DIR": lease.config_dir},
                                 effort=effort)
            text, _ = client._run_prompt(prompt, timeout_seconds=timeout, follow_up=follow_up,
                                         on_tool_activity=_tool_activity_writer(task_id, board, run_id, client))
            if not subs.is_usage_limit_error(text) and not subs.is_auth_error(text):
                return text, client, lease.name
            limited = text
        except Exception as exc:
            if not subs.is_usage_limit_error(str(exc)) and not subs.is_auth_error(str(exc)): raise
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
    prompt=("You are the sole native external coding-harness session for this already-scoped task. Work only in the supplied cwd; do not orchestrate child tasks. Follow project rules and return a concise factual handoff with tests run. Never create test fixtures on the live kanban board: tests that need a board must spin up an isolated one (set HERMES_KANBAN_HOME to a temp dir) and clean it up in teardown. If the cwd is a git repository and you changed files: run the relevant tests and COMMIT your work (conventional-commits message referencing the task id) before finishing - completing a code task with a dirty tree is a protocol violation; do not push.\n\n"+context)
    try:
        timeout=float(os.getenv("HERMES_ACP_TIMEOUT_SECONDS", "3600"))
        model=os.getenv("HERMES_KANBAN_MODEL","").strip() or None
        effort=os.getenv("HERMES_KANBAN_EFFORT","").strip() or None
        subscription=None
        follow_up=(lambda: _drain_steer(task_id, board)) if _steering_enabled() else None
        if executor == "claude-code":
            text,client,subscription=_run_claude_code_session(command,args,workspace,prompt,timeout,model,task_id,follow_up,board,effort,run_id)
        else:
            client=_new_client(command,args,workspace,model,effort=effort)
            text,_=client._run_prompt(prompt,timeout_seconds=timeout,follow_up=follow_up,
                                      on_tool_activity=_tool_activity_writer(task_id,board,run_id,client))
        _report_usage(client,executor,task_id,subscription)
    except Exception as exc:
        with kb.connect_closing(board=board) as conn: kb.block_task(conn,task_id,reason=f"External {executor} ACP session failed: {exc}",kind="capability",expected_run_id=run_id)
        raise
    metadata={"executor":executor,"acp_command":command,"provider":f"acp-{executor}","workspace":workspace}
    run_model=getattr(client,"last_model","") or model
    if run_model: metadata["model"]=run_model
    if effort: metadata["effort_requested"]=effort
    if subscription: metadata["subscription"]=subscription
    _stamp_session_metadata(metadata, client)
    try:
        tool_calls=client.tool_activity_snapshot()
    except Exception:
        tool_calls=[]
    if tool_calls: metadata["tool_calls"]=tool_calls
    with kb.connect_closing(board=board) as conn:
        if not kb.complete_task(conn,task_id,summary=text.strip() or f"External {executor} ACP session completed.",metadata=metadata,expected_run_id=run_id): raise RuntimeError("task was reclaimed or terminal")
    return text
def main():
    return run_task(executor=os.environ["HERMES_KANBAN_EXECUTOR"],task_id=os.environ["HERMES_KANBAN_TASK"],workspace=os.environ["HERMES_KANBAN_WORKSPACE"],board=os.getenv("HERMES_KANBAN_BOARD")) and 0
if __name__ == "__main__": main()
