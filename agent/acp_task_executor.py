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
def _report_usage(client, executor, task_id):
    """Best-effort turn accounting: the post_api_request hook lands usage in zeus.db token_usage."""
    usage = getattr(client, "last_turn_usage", None)
    if not usage: return
    try:
        from hermes_cli.plugins import discover_plugins, invoke_hook
        discover_plugins()
        invoke_hook("post_api_request", task_id=task_id,
                    session_id=getattr(client, "last_session_id", "") or "",
                    provider=f"acp-{executor}", api_mode="acp",
                    model=getattr(client, "last_model", "") or executor, usage=usage)
    except Exception:
        pass
def run_task(*, executor, task_id, workspace, board=None):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(board=board) as conn:
        task=kb.get_task(conn, task_id)
        if not task: raise ValueError(f"unknown task {task_id}")
        context, run_id = kb.build_worker_context(conn, task_id), task.current_run_id
    command,args=command_for(executor)
    prompt=("You are the sole native external coding-harness session for this already-scoped task. Work only in the supplied cwd; do not orchestrate child tasks. Follow project rules and return a concise factual handoff with tests run.\n\n"+context)
    try:
        timeout=float(os.getenv("HERMES_ACP_TIMEOUT_SECONDS", "3600"))
        model=os.getenv("HERMES_KANBAN_MODEL","").strip() or None
        client=CopilotACPClient(acp_command=command,acp_args=args,acp_cwd=workspace,allow_permissions=True,session_model=model)
        text,_=client._run_prompt(prompt,timeout_seconds=timeout)
        _report_usage(client,executor,task_id)
    except Exception as exc:
        with kb.connect_closing(board=board) as conn: kb.block_task(conn,task_id,reason=f"External {executor} ACP session failed: {exc}",kind="capability",expected_run_id=run_id)
        raise
    with kb.connect_closing(board=board) as conn:
        if not kb.complete_task(conn,task_id,summary=text.strip() or f"External {executor} ACP session completed.",metadata={"executor":executor,"acp_command":command},expected_run_id=run_id): raise RuntimeError("task was reclaimed or terminal")
    return text
def main():
    return run_task(executor=os.environ["HERMES_KANBAN_EXECUTOR"],task_id=os.environ["HERMES_KANBAN_TASK"],workspace=os.environ["HERMES_KANBAN_WORKSPACE"],board=os.getenv("HERMES_KANBAN_BOARD")) and 0
if __name__ == "__main__": main()
