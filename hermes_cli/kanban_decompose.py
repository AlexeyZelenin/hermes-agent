"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb


logger = logging.getLogger(__name__)


_DEFAULT_CONTEXT_BUDGET_TOKENS = 150_000
_MIN_CONTEXT_BUDGET_TOKENS = 32_000


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks. Decomposition is an estimate and
planning action only: it must never assign work or start a worker.

You will be given:
  - The original task title and body
  - A target maximum context budget for one fresh worker session

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "parents": [<int>, ...],
        "estimated_context_tokens": <rough integer estimate>,
        "model_tier": "cheap" | "mid" | "standard" | "strong",
        "model_rationale": "<one short clause on why this tier>"
      },
      ...
    ]
  }

Rules:
  - The context-budget estimate is intentionally rough, not a promise. It
    includes the worker's base prompt, task spec, likely repository/file/tool
    output, and implementation conversation. Keep every child at or below the
    supplied budget with sensible margin.
  - Optional calibration context may summarize outcomes from similar completed
    tasks. Use it to calibrate estimates and their range, but never treat it as
    a point guarantee. The current task's scope and uncertainty take priority.
  - If no calibration context is present, estimate normally from the task
    description; do not claim that historical evidence exists.
  - First look for semantic seams. Split only at those seams; never create
    arbitrary micro-tasks merely to hit a number.
  - Among valid semantic splits, choose the FEWEST workers and the LARGEST
    pieces that fit the budget. The objective is minimum handoffs and minimum
    duplicate worker context, not maximum parallelism.
  - If the whole task is plausibly within budget, return fanout=false. If a
    semantic child would exceed budget, split that child further at its own
    semantic seams before returning the graph.
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism only when it does not add a handoff or duplicate work.
    If two tasks can be done independently, give them no parents so the
    dispatcher can fan them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Do not cram
    clearly over-budget work into 1 task.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.
  - "model_tier" assigns each chunk a capability tier from the operator's
    four-tier model grid (ordered cheapest → most capable). Model choice is
    planner intelligence, not a per-task human setting: judge each chunk on its
    own merits.
      * "cheap"    — Tier 4 (quick): mechanical, low-ambiguity work with a clear
        recipe — translations, templated/repetitive edits, renames, status
        checks, formatting, config value changes, boilerplate from an exact
        spec.
      * "mid"      — Tier 3 (balanced): well-defined implementation, routine
        refactors, test writing, documentation, moderate bug fixes — scoped
        work that does not need the default's headroom.
      * "standard" — Tier 2 (workhorse, the DEFAULT): complex but bounded coding
        whose shape is clear and needs no long planning horizon. Use it when
        unsure.
      * "strong"   — Tier 1 (frontier): high-ambiguity or high-blast-radius work
        — architecture and API design, cross-cutting refactors, long-horizon
        autonomous work, security-sensitive changes, debugging of unknown depth,
        work whose spec must be interpreted.
    Judge by the NATURE of the work, not its size: a long translation is still
    "cheap"; a one-line fix in an unfamiliar concurrency path is "strong".
    Prefer "standard"/"mid" for ordinary work and reserve "strong" for chunks
    that genuinely need the frontier tier — it is the most expensive.
  - "model_rationale" is a single short clause (<= 120 chars) justifying the
    tier for THIS chunk (e.g. "cross-cutting refactor, spec must be
    interpreted"). It is recorded on the child task so the assignment is
    auditable.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "estimated_context_tokens": <rough integer estimate>
  }

In that case the task stays as one work item with a tightened spec. It remains
unassigned until an explicit take or batch-take action.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Maximum rough context budget per fresh worker: {context_budget_tokens:,} tokens
{calibration_context}"""


def _task_model_map(task) -> dict:
    """Effective model map for this task's chunks: global ← board ← project."""
    project_models = None
    if getattr(task, "project_id", None):
        try:
            from hermes_cli import projects_db as pdb
            with pdb.connect_closing() as pconn:
                proj = pdb.get_project(pconn, task.project_id)
            project_models = proj.models if proj else None
        except Exception as exc:
            logger.debug("decompose: project model map unavailable: %s", exc)
    return kb.resolve_model_map(None, project_models)


def _calibration_context(task_id: str, title: str, body: str,
                         context_budget_tokens: int) -> str:
    """Ask optional plugins for bounded, aggregate-only estimate calibration.

    The decomposer remains usable without plugins.  A plugin may return either
    a string or ``{"context": "..."}``; raw history is deliberately not a
    core concern and callers cap the injected aggregate to keep this auxiliary
    prompt bounded.
    """
    try:
        from hermes_cli.plugins import invoke_hook
        results = invoke_hook(
            "kanban_decomposer_context",
            task_id=task_id,
            title=title,
            body=body,
            context_budget_tokens=context_budget_tokens,
        )
    except Exception as exc:
        logger.debug("decompose: calibration context hook failed: %s", exc)
        return ""

    parts: list[str] = []
    for result in results or []:
        if isinstance(result, dict):
            result = result.get("context", "")
        if isinstance(result, str) and result.strip():
            parts.append(result.strip())
    if not parts:
        return ""
    return "\n\nCalibration context:\n" + "\n\n".join(parts)[:4_000] + "\n"


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None
    # One entry per child on a fanout: {"child_id", "title", "tier", "model",
    # "rationale"}. "model" is None when the tier inherits the board default.
    model_assignments: list[dict] | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}



def _resolve_context_budget_tokens(cfg: dict) -> int:
    """Resolve the decomposer's coarse per-worker context cap.

    This is a planning guardrail, not a runtime token counter: actual context
    varies with tool calls and model behavior. Keep a defensible lower bound so
    malformed configuration cannot force pathological micro-task fanout.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    raw = kanban_cfg.get("decomposer_context_budget_tokens")
    if raw is None:
        return _DEFAULT_CONTEXT_BUDGET_TOKENS
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT_BUDGET_TOKENS
    return max(_MIN_CONTEXT_BUDGET_TOKENS, budget)



def _format_assignment_comment(tier: str, model: Optional[str],
                               rationale: str) -> str:
    """One-line, human-readable model-assignment note for a child task."""
    target = model or "board default (standard tier)"
    line = f"Model tier: {tier} → {target}"
    if rationale:
        line += f" — {rationale}"
    return line


def _record_model_assignments(child_ids: list[str], tier_meta: list[dict],
                              author: str) -> list[dict]:
    """Comment the tier assignment on each child; return the assignment list.

    Best-effort and fail-open: a per-child comment failure is logged and
    skipped so a successful decomposition is never rolled back by an audit
    write.  ``tier_meta`` is parallel to ``child_ids`` (same input order).
    """
    assignments: list[dict] = []
    for child_id, meta in zip(child_ids, tier_meta):
        assignments.append({
            "child_id": child_id,
            "title": meta["title"],
            "tier": meta["tier"],
            "model": meta["model"],
            "rationale": meta["rationale"],
        })
        comment = _format_assignment_comment(
            meta["tier"], meta["model"], meta["rationale"],
        )
        try:
            with kb.connect_closing() as conn:
                kb.add_comment(conn, child_id, author, comment)
        except Exception as exc:  # noqa: BLE001 — audit write must never break decompose
            logger.debug(
                "decompose: model-assignment comment failed for %s: %s",
                child_id, exc,
            )
    return assignments


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    context_budget_tokens = _resolve_context_budget_tokens(cfg)
    model_map = _task_model_map(task)

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    title = _truncate(task.title or "", 400)
    body = _truncate(task.body or "(no body)", 4000)
    calibration_context = _calibration_context(
        task.id, title, body, context_budget_tokens,
    )
    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=title,
        body=body,
        context_budget_tokens=context_budget_tokens,
        calibration_context=calibration_context,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        resp = call_llm(
            task="kanban_decomposer",
            # Board/project "aux" model (if configured) overrides the
            # auxiliary.kanban_decomposer.* model from config.yaml.
            model=model_map.get("aux"),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    fanout = bool(parsed.get("fanout"))
    audit_author = author or _profile_author()

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None

        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=None,
                author=audit_author,
                auto_promote=False,
            )
        if not ok:
            return DecomposeOutcome(
                task_id, False, "task moved out of triage before promotion",
            )
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False, new_title=title_val,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )

    # Assignment is deliberately outside decomposition. The later explicit
    # take / batch-take action selects assignees for the whole graph.
    children: list[dict] = []
    # Parallel to ``children`` (same index order): per-child tier metadata used
    # to record an auditable model-assignment comment after the children exist.
    tier_meta: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}] is not an object",
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].title is missing or empty",
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""

        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        # Clean parent indices: drop non-int and out-of-range.
        clean_parents = [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx]
        # Tier → model via the board/project map. "standard" (or an
        # unmapped tier) leaves the child on the default model chain — the
        # Tier-2 workhorse. cheap/mid/strong resolve to their mapped models.
        tier = str(entry.get("model_tier") or "").strip().lower()
        if tier not in ("cheap", "mid", "standard", "strong"):
            tier = "standard"
        tier_model = model_map.get(tier) if tier in ("cheap", "mid", "strong") else None
        rationale = entry.get("model_rationale")
        rationale = rationale.strip()[:200] if isinstance(rationale, str) else ""
        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": None,
            "parents": clean_parents,
            "model_override": tier_model,
        })
        tier_meta.append({
            "title": title.strip()[:200],
            "tier": tier,
            "model": tier_model,
            "rationale": rationale,
        })

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=None,
                children=children,
                author=audit_author,
                auto_promote=False,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    # Record the model-tier assignment (with the planner's one-line rationale)
    # as a comment on each child, so a taken batch shows tier-differentiated
    # assignments that are auditable in the task timeline. Best-effort: a
    # comment failure must never undo a successful decomposition.
    assignments = _record_model_assignments(child_ids, tier_meta, audit_author)

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids, model_assignments=assignments,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
