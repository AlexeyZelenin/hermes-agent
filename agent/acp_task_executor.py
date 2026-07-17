"""One-session external ACP executor for Kanban tasks; separate from providers."""
from __future__ import annotations
import os, shlex
from agent.copilot_acp_client import CopilotACPClient

_DEFAULT = {"claude-code": ("npx", ["--yes", "@agentclientprotocol/claude-agent-acp"]), "codex": ("codex-acp", ["--stdio"])}
def command_for(executor):
    if executor not in _DEFAULT: raise ValueError(f"unsupported ACP executor: {executor}")
    prefix = "HERMES_CLAUDE_CODE_ACP" if executor == "claude-code" else "HERMES_CODEX_ACP"
    command = os.getenv(prefix + "_COMMAND", "").strip() or _DEFAULT[executor][0]
    raw = os.getenv(prefix + "_ARGS", "").strip()
    return command, shlex.split(raw) if raw else list(_DEFAULT[executor][1])
def _report_usage(client, executor, task_id, subscription=None):
    """Best-effort turn accounting: the post_api_request hook lands usage in zeus.db token_usage."""
    usage = getattr(client, "last_turn_usage", None)
    if not usage: return
    try:
        from hermes_cli.plugins import discover_plugins, invoke_hook
        discover_plugins()
        invoke_hook("post_api_request", task_id=task_id,
                    session_id=getattr(client, "last_session_id", "") or "",
                    provider=f"acp-{executor}", api_mode="acp",
                    model=getattr(client, "last_model", "") or executor, usage=usage,
                    subscription=subscription or "")
    except Exception:
        pass
def _new_client(command, args, workspace, model, extra_env=None):
    return CopilotACPClient(acp_command=command, acp_args=args, acp_cwd=workspace,
                            allow_permissions=True, session_model=model,
                            extra_env=extra_env)
def _run_claude_code_session(command, args, workspace, prompt, timeout, model, task_id):
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
        client = _new_client(command, args, workspace, model)
        text, _ = client._run_prompt(prompt, timeout_seconds=timeout)
        return text, client, None
    while True:
        lease = subs.acquire(task_id=task_id)
        limited = None
        try:
            client = _new_client(command, args, workspace, model,
                                 extra_env={"CLAUDE_CONFIG_DIR": lease.config_dir})
            text, _ = client._run_prompt(prompt, timeout_seconds=timeout)
            if not subs.is_usage_limit_error(text) and not subs.is_auth_error(text):
                return text, client, lease.name
            limited = text
        except Exception as exc:
            if not subs.is_usage_limit_error(str(exc)) and not subs.is_auth_error(str(exc)): raise
            limited = str(exc)
        finally:
            subs.release(lease)
        subs.mark_limited(lease.name, limited)
def run_task(*, executor, task_id, workspace, board=None):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(board=board) as conn:
        task=kb.get_task(conn, task_id)
        if not task: raise ValueError(f"unknown task {task_id}")
        context, run_id = kb.build_worker_context(conn, task_id), task.current_run_id
    command,args=command_for(executor)
    prompt=("You are the sole native external coding-harness session for this already-scoped task. Work only in the supplied cwd; do not orchestrate child tasks. Follow project rules and return a concise factual handoff with tests run. Never create test fixtures on the live kanban board: tests that need a board must spin up an isolated one (set HERMES_KANBAN_HOME to a temp dir) and clean it up in teardown.\n\n"+context)
    try:
        timeout=float(os.getenv("HERMES_ACP_TIMEOUT_SECONDS", "3600"))
        model=os.getenv("HERMES_KANBAN_MODEL","").strip() or None
        subscription=None
        if executor == "claude-code":
            text,client,subscription=_run_claude_code_session(command,args,workspace,prompt,timeout,model,task_id)
        else:
            client=_new_client(command,args,workspace,model)
            text,_=client._run_prompt(prompt,timeout_seconds=timeout)
        _report_usage(client,executor,task_id,subscription)
    except Exception as exc:
        with kb.connect_closing(board=board) as conn: kb.block_task(conn,task_id,reason=f"External {executor} ACP session failed: {exc}",kind="capability",expected_run_id=run_id)
        raise
    metadata={"executor":executor,"acp_command":command}
    if subscription: metadata["subscription"]=subscription
    with kb.connect_closing(board=board) as conn:
        if not kb.complete_task(conn,task_id,summary=text.strip() or f"External {executor} ACP session completed.",metadata=metadata,expected_run_id=run_id): raise RuntimeError("task was reclaimed or terminal")
    return text
def main():
    return run_task(executor=os.environ["HERMES_KANBAN_EXECUTOR"],task_id=os.environ["HERMES_KANBAN_TASK"],workspace=os.environ["HERMES_KANBAN_WORKSPACE"],board=os.getenv("HERMES_KANBAN_BOARD")) and 0
if __name__ == "__main__": main()
