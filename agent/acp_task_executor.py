"""One-session external ACP executor for Kanban tasks; separate from providers."""
from __future__ import annotations
import json, os, re, shlex
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
_PLACEHOLDER_MODELS = frozenset({"default", "inherit", "auto", "none", ""})
def _clean_model(value):
    """Concrete model id, or None for a falsy/placeholder token. Keeps
    'default'/'' out of run metadata + the token ledger so the card's model
    line never shows a fake name (t_3f79b87d)."""
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in _PLACEHOLDER_MODELS:
        return None
    return text
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
def _make_usage_sink(task_id, executor, subscription=None):
    """Build a CopilotACPClient usage_sink that lands per-turn token usage in
    zeus.db token_usage via the post_api_request plugin hook AS THE RUN
    PROGRESSES. The client fires it live: a provisional total-only row on each
    growing usage_update (so a running card shows climbing tokens instead of 0)
    and a reconciling billing-split row at each turn settle. Recording per turn
    replaces the old fire-once-at-session-end path, so a running acp-claude-code
    task no longer reads 0 live tokens. Best-effort: a hook failure never
    disturbs the ACP session.

    The metered API-hook path is not double-counted: an ACP subprocess talks to
    the provider directly and never crosses the Hermes API, so this hook is the
    ONLY writer for the session - the two paths are mutually exclusive."""
    from hermes_cli.plugins import discover_plugins, invoke_hook
    try:
        discover_plugins()
    except Exception:
        pass

    def _sink(payload):
        usage = payload.get("usage") or {}
        if not any(usage.get(k) for k in ("total_tokens", "input_tokens", "output_tokens")):
            return
        context = payload.get("context") or {}
        try:
            invoke_hook("post_api_request", task_id=task_id,
                        session_id=payload.get("session_id") or "",
                        provider=f"acp-{executor}", api_mode="acp",
                        model=_clean_model(payload.get("model")) or executor, usage=usage,
                        subscription=subscription or "",
                        effort=payload.get("effort") or "",
                        context_used=context.get("context_used"),
                        context_size=context.get("context_size"),
                        cost_usd=context.get("cost_usd"))
        except Exception:
            pass
    return _sink
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
                tool_activity_sink=None, usage_sink=None):
    return CopilotACPClient(acp_command=command, acp_args=args, acp_cwd=workspace,
                            allow_permissions=True, session_model=model,
                            session_effort=effort, extra_env=extra_env,
                            tool_activity_sink=tool_activity_sink,
                            usage_sink=usage_sink)
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
# One worker subscription pool spans vendors: a Claude pocket pins
# CLAUDE_CONFIG_DIR, a Codex pocket pins CODEX_HOME. The env var and the ACP
# executor to spawn are the only per-vendor differences on this path (task
# t_2eec7f4a) - lease/rotation/cooldown/pacing are provider-blind.
_PROVIDER_ENV_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_PROVIDER_EXECUTOR = {"claude": "claude-code", "codex": "codex"}
def _provider_for_executor(executor):
    from agent import claude_subscriptions as subs
    return subs.PROVIDER_CODEX if executor == "codex" else subs.PROVIDER_CLAUDE
def _cross_vendor_enabled():
    """Whether a task may fall over to another vendor's pocket when its own
    vendor's pool is fully cooling (task t_2eec7f4a deliverable 4). Off by
    default: a vendor-tuned task stays on its vendor unless the operator opts in
    with HERMES_POOL_CROSS_VENDOR, since command + config-dir are then driven by
    the leased pocket's provider (Codex command + CODEX_HOME for a Codex lease)."""
    return os.getenv("HERMES_POOL_CROSS_VENDOR", "0").strip().lower() in ("1", "true", "yes", "on")
def _acquire_pocket(subs, task_id, provider):
    """Lease a pocket of ``provider``; fall over to the whole pool (any vendor)
    when that vendor is exhausted and cross-vendor routing is enabled."""
    try:
        return subs.acquire(task_id=task_id, provider=provider)
    except subs.NoSubscriptionAvailable:
        if provider is not None and _cross_vendor_enabled():
            return subs.acquire(task_id=task_id, provider=None)
        raise
def _run_pooled_session(executor, workspace, prompt, timeout, model, task_id, follow_up=None, board=None, effort=None, tool_activity_sink=None, run_id=None):
    """Run the prompt on the vendor-agnostic subscription pool, rotating on limits.

    ``executor`` picks the desired vendor (claude-code -> Claude pockets, codex
    -> Codex pockets). Each session pins the leased pocket's config dir via the
    provider's env var (CLAUDE_CONFIG_DIR / CODEX_HOME) and spawns that vendor's
    ACP command ('spread' strategy). A session that dies on a usage/auth limit
    puts that pocket into cooldown and the task retries immediately on the next
    pocket of the same vendor; NoSubscriptionAvailable propagates when the whole
    vendor pool is cooling. An empty vendor pool (no login dirs on this host)
    falls back to the legacy single-session path with the CLI's default dir.
    """
    from agent import claude_subscriptions as subs
    provider = _provider_for_executor(executor)
    if subs.pool_size(provider) == 0:
        command, args = command_for(executor)
        extra_env = _contributed_worker_env(task_id, board, None, run_id) or None
        client = _new_client(command, args, workspace, model, extra_env=extra_env,
                             effort=effort, tool_activity_sink=tool_activity_sink,
                             usage_sink=_make_usage_sink(task_id, executor, None))
        text, _ = client._run_prompt(prompt, timeout_seconds=timeout, follow_up=follow_up)
        return text, client, None
    while True:
        lease = _acquire_pocket(subs, task_id, provider)
        command, args = command_for(_PROVIDER_EXECUTOR.get(lease.provider, executor))
        env_var = _PROVIDER_ENV_VAR.get(lease.provider, "CLAUDE_CONFIG_DIR")
        limited = None
        client = None
        try:
            # Plugin-contributed env (e.g. Zeus Langfuse OTLP) first, then the
            # core-owned config-dir var last so the leased pocket's dir always
            # wins over any contributed key.
            extra_env = _contributed_worker_env(task_id, board, lease.name, run_id)
            extra_env[env_var] = lease.config_dir
            client = _new_client(command, args, workspace, model,
                                 extra_env=extra_env,
                                 effort=effort, tool_activity_sink=tool_activity_sink,
                                 usage_sink=_make_usage_sink(task_id, executor, lease.name))
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
def _claim_base_commit(run_id, board=None):
    """The run's claim-time base commit, or None when unknown/unrecorded."""
    if run_id is None:
        return None
    from hermes_cli import kanban_db as kb
    try:
        with kb.connect_closing(board=board) as conn:
            row = conn.execute(
                "SELECT base_commit FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
    except Exception:
        return None
    return row["base_commit"] if row else None
def _git_work_state(workspace, run_id, board=None):
    """Structural facts about what the run left behind: ``repo`` (the workspace
    resolves to a git repo), ``dirty`` (working tree has changes) and
    ``committed`` (a commit landed past the run's claim-time base). ``dirty`` /
    ``committed`` are None when git could not answer - callers fail open on
    those so a transient git error never discards a real run."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    top = kb._git_toplevel(Path(workspace))
    if top is None:
        return {"repo": False, "dirty": None, "committed": None}
    entries = kb._git_status_porcelain_entries(top)
    base = _claim_base_commit(run_id, board)
    return {"repo": True,
            "dirty": None if entries is None else bool(entries),
            "committed": kb._git_has_new_commit(top, base) if base else None}
def _workspace_shows_work(workspace, run_id, board=None):
    """True when this run left visible work in a git-backed workspace - a dirty
    tree or a new commit past the run's claim-time base. A scratch workspace is
    not a git repo, so there is nothing to show and this returns False.

    This is the second half of the phantom-completion guard: it lets a run that
    committed real code but returned an empty handoff string (e.g. crashed right
    after committing) still complete, while a run that produced neither output
    nor changes is refused (t_790b2481)."""
    state = _git_work_state(workspace, run_id, board)
    if not state["repo"]:
        return False
    if state["dirty"] is None or state["dirty"]:
        return True
    return bool(state["committed"])
def _landed_clean_commit(state):
    """True when the run's work is durably landed: a git workspace with a clean
    tree and a new commit past the run's base. This is what makes an otherwise
    unfinished-looking session safe to complete - the code is in git, so nothing
    is lost even though the worker stopped mid-thought."""
    return bool(state["repo"] and state["committed"] and state["dirty"] is False)
# Terminal handoff marker (t_222f1e4a). The worker prompt asks for it as the
# final line; its presence is the one POSITIVE completion signal the executor
# trusts. Absence alone is NOT fatal - prompt compliance is not guaranteed and a
# missing marker must never discard real work - it only enables the
# unfinished-turn check below.
HANDOFF_MARKER = "HANDOFF-COMPLETE"
_TAIL_CHARS = 400  # closing window scanned for the marker / closing sentence
# A session that stops mid-flight ends on an intent or a wait ("I'll report when
# it lands", "Let me run the tests"), not on a report. Matched against the LAST
# sentence only: a mid-report mention of waiting is normal prose, while the
# closing sentence is what the session actually ended on. Substring-scanning the
# whole handoff is the anti-pattern that once discarded committed work.
_UNFINISHED_PATTERNS = (
    r"\bi'?ll (report|update|circle back|check back|follow up|wait\b|let it|keep)",
    r"\bwill report (back|when)\b",
    r"\bwaiting (on|for) (the|it|that|this|my|a|an)\b",
    r"\breport(ing)? (back )?(when|once) it\b",
    r"\bstand(ing)? by\b",
    r"^(let me|now (i'?ll|let me)|next,? i'?ll|i'?ll now)\b",
    r"\b(жду|подожду|дождусь|доложу|отчитаюсь)\b",
    r"^(сейчас|запущу|проверю|посмотрю)\b",
)
# ... unless the wait is on the OPERATOR (a legitimate handoff ends that way).
_OPERATOR_WAIT = r"\b(you|your|operator|reviewer|review|approval|merge|decision)\b"
_FOOTER_LINE = r"^\[[^\]]*\]$"
def _final_sentence(text):
    """Last sentence of the session's closing line, skipping the slash-command
    footer convention (``[/command]``). The incident handoff arrived as one
    unbroken paragraph, so splitting on lines alone is not enough."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    while lines and re.match(_FOOTER_LINE, lines[-1]):
        lines.pop()
    if not lines:
        return ""
    return re.split(r"(?<=[.!?…])\s+", lines[-1])[-1].strip()[-_TAIL_CHARS:]
def _strip_marker(text):
    """The handoff text with the terminal marker removed - the marker is a
    protocol token for the guard, not something a reviewer needs on the card.
    The signal itself is kept on the run metadata."""
    return "\n".join(ln for ln in (text or "").strip().splitlines()
                     if ln.strip() != HANDOFF_MARKER).strip()
def _looks_unfinished(text):
    """True when the session's closing sentence reads as work-in-progress rather
    than a handoff - the self-pacing 'I'll report when it lands' turn that ended
    an ACP session mid-task and still marked it done (t_222f1e4a)."""
    sentence = _final_sentence(text).lower()
    if not sentence or re.search(_OPERATOR_WAIT, sentence):
        return False
    return any(re.search(p, sentence) for p in _UNFINISHED_PATTERNS)
def _completion_verdict(text, workspace, run_id, board=None):
    """Classify an ACP session's output as a real completion or a phantom.

    Returns ``(signal, refusal)`` - ``refusal`` is None to complete, else the
    phrase naming why completion is refused. Signals, strongest first:

    * ``marker``  - the terminal handoff marker is present: trust it.
    * ``unfinished`` - the closing sentence is a wait/intent, so the session
      ended mid-flight. Completing here is the phantom-done this guard exists to
      kill; refused unless the run nonetheless landed its work durably (clean
      tree + new commit), which downgrades it to ``unfinished_committed``.
    * ``empty`` - no text at all and no visible work (t_790b2481).
    * ``text`` - a plain handoff with no marker: completes, and the signal is
      stamped on the run so marker-less completions stay measurable.
    """
    body = (text or "").strip()
    if HANDOFF_MARKER in body[-_TAIL_CHARS:]:
        return "marker", None
    if _looks_unfinished(body):
        if _landed_clean_commit(_git_work_state(workspace, run_id, board)):
            return "unfinished_committed", None
        return "unfinished", (
            "ended mid-task on an unfinished turn with no committed work "
            f"(last message: {_final_sentence(body)!r})"
        )
    if not body and not _workspace_shows_work(workspace, run_id, board):
        return "empty", "produced zero output and no changes"
    return "text", None
def run_task(*, executor, task_id, workspace, board=None):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(board=board) as conn:
        task=kb.get_task(conn, task_id)
        if not task: raise ValueError(f"unknown task {task_id}")
        context, run_id = kb.build_worker_context(conn, task_id), task.current_run_id
    command,args=command_for(executor)
    prompt=("You are the sole native external coding-harness session for this already-scoped task. Work only in the supplied cwd; do not orchestrate child tasks. Follow project rules and return a concise factual handoff with tests run. Never create test fixtures against live shared or host state: tests that need a kanban board must spin up an isolated one (set HERMES_KANBAN_HOME to a temp dir); tests that touch the OS keychain, credential stores, or other host state must use a temporary/throwaway store (e.g. a temp keychain via `security create-keychain`) or mock the calls - NEVER the real login keychain or live data, which prompts the user and pollutes their system. Clean up in teardown. If the cwd is a git repository and you changed files: run the relevant tests and COMMIT your work (conventional-commits message referencing the task id) before finishing - completing a code task with a dirty tree is a protocol violation; do not push.\n\nFinishing protocol: this session ENDS when your turn ends - there is no later turn to come back on. Never end a turn waiting on background work and never schedule a wake-up to resume yourself: commit BEFORE you start any verification you intend to leave running, and if a check is still unfinished when you stop, say plainly what is unverified. End your final message with the single line "+HANDOFF_MARKER+" so completion is distinguishable from a session that stopped mid-thought.\n\n"+context)
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
        if executor in ("claude-code", "codex"):
            # Both vendors run through the one worker pool: lease a pocket of the
            # matching provider (pinning CLAUDE_CONFIG_DIR / CODEX_HOME) with
            # limit rotation. An empty vendor pool degrades to a bare session.
            text,client,subscription=_run_pooled_session(executor,workspace,prompt,timeout,model,task_id,follow_up,board,effort,tool_sink,run_id)
        else:
            client=_new_client(command,args,workspace,model,effort=effort,tool_activity_sink=tool_sink,
                               usage_sink=_make_usage_sink(task_id,executor,None))
            text,_=client._run_prompt(prompt,timeout_seconds=timeout,follow_up=follow_up)
    except Exception as exc:
        with kb.connect_closing(board=board) as conn: kb.block_task(conn,task_id,reason=f"External {executor} ACP session failed: {exc}",kind="capability",expected_run_id=run_id)
        raise
    metadata={"executor":executor,"acp_command":command,"provider":f"acp-{executor}","workspace":workspace}
    run_model=_clean_model(getattr(client,"last_model","")) or _clean_model(model)
    if run_model: metadata["model"]=run_model
    if effort: metadata["effort_requested"]=effort
    if subscription: metadata["subscription"]=subscription
    _stamp_session_metadata(metadata, client)
    # Phantom-completion guard (t_790b2481, widened by t_222f1e4a): a zero-output
    # run with nothing to show, and a run whose session ended mid-task on a
    # waiting/self-pacing turn without committing, are both false positives.
    # Completing either marks the task done with a narrative in place of a
    # handoff, so re-block for another attempt instead. Runs with a real handoff
    # (or durably committed work) complete unchanged.
    signal, refusal = _completion_verdict(text, workspace, run_id, board)
    metadata["handoff_signal"] = signal
    if refusal:
        with kb.connect_closing(board=board) as conn:
            kb.block_task(conn, task_id, kind="capability", expected_run_id=run_id,
                          reason=f"External {executor} ACP session {refusal} (phantom completion blocked)")
        raise RuntimeError(f"External {executor} ACP session {refusal}; completion blocked")
    summary = _strip_marker(text) or f"External {executor} ACP session completed."
    try:
        with kb.connect_closing(board=board) as conn:
            if not kb.complete_task(conn,task_id,summary=summary,metadata=metadata,expected_run_id=run_id): raise RuntimeError("task was reclaimed or terminal")
    except kb.UncommittedWorkError as exc:
        # The DoD gate refused (dirty tree / no commit). Without this the worker
        # process would just die with the task still 'running' and the reason
        # buried in a traceback; block it so the card names what to fix.
        with kb.connect_closing(board=board) as conn:
            kb.block_task(conn, task_id, kind="capability", expected_run_id=run_id,
                          reason=f"DoD gate refused completion ({exc.kind}): {exc.detail}")
        raise
    return text
def main():
    return run_task(executor=os.environ["HERMES_KANBAN_EXECUTOR"],task_id=os.environ["HERMES_KANBAN_TASK"],workspace=os.environ["HERMES_KANBAN_WORKSPACE"],board=os.getenv("HERMES_KANBAN_BOARD")) and 0
if __name__ == "__main__": main()
