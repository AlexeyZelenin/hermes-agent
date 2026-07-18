"""Kanban triage specifier — flesh out a one-liner into a real spec.

Used by ``hermes kanban specify [task_id | --all]``. Takes a task that
lives in the Triage column (a rough idea, typically only a title), calls
the auxiliary LLM to produce:

  * A tightened title (optional — only replaces if the model proposes a
    materially different one)
  * A concrete body: goal, proposed approach, acceptance criteria

and then flips the task ``triage -> todo`` via
``kanban_db.specify_triage_task``. The dispatcher promotes it to
``ready`` on its next tick (or immediately if there are no open parents).

Design notes
------------

* This module intentionally mirrors ``hermes_cli/goals.py`` — same aux
  client pattern, same "empty config => skip, don't crash" tolerance.
  Keeps the surface area tiny and the failure modes predictable.

* The prompt is a short system + user pair. We ask for JSON with
  ``{title, body}``; if parsing fails, we fall back to treating the
  whole response as the body and leave the title untouched. No
  retry loop — one shot, keep cost bounded.

* Structured output / JSON mode is not requested explicitly so the
  specifier works on providers that don't implement it. The parse
  is lenient (tolerates markdown code fences around the JSON).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import vision as vision_mod

from utils import env_int

HERMES_KANBAN_SPECIFY_MAX_TOKENS = max(
    1500,
    env_int("HERMES_KANBAN_SPECIFY_MAX_TOKENS", 6000),
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban triage specifier for the Hermes Agent board.
A user dropped a rough idea into the Triage column. Your job is to turn it
into a concrete, actionable task spec that an autonomous worker can pick up
and execute without further clarification.

Output a single JSON object with exactly two keys:

  {
    "title": "<tightened task title, <= 80 chars, imperative voice>",
    "body":  "<multi-line spec, see structure below>"
  }

The body MUST include these sections, each prefixed with a bold markdown
heading, in this order:

  **Goal** — one sentence, user-facing outcome.
  **Approach** — 2-5 bullets on how a worker should tackle it.
  **Acceptance criteria** — checklist of concrete, verifiable conditions.
  **Out of scope** — short list of things NOT to touch (omit if nothing
      obvious; never invent scope creep).

Rules:
  - Keep the tightened title close in meaning to the original idea — do
    NOT invent a different project.
  - If the original idea is already detailed, preserve its substance and
    just reformat into the sections above.
  - Never add invented requirements the user didn't hint at.
  - No preamble, no closing remarks, no code fences around the JSON.
  - Output only the JSON object and nothing else.
"""


_USER_TEMPLATE = """Task id: {task_id}
Current title: {title}
Current body:
{body}
"""


# Appended to the system prompt only when a vision doc is available. Hands the
# gate to the planner: before promoting a card, it must judge fit against the
# project vision and emit a verdict the caller can act on.
_VISION_SYSTEM_EXTRA = """

VISION CHECK — the project vision document is provided below in the user
message. Before finalising, judge whether this task fits the picture:
  - Does it advance what we are building, or is it out of scope / redundant?
  - Is it a HALF-SOLUTION the vision warns against (e.g. "a local repo and
    that's it" when the vision calls for a finished pipeline)?
  - Does it CONTRADICT the "What we do NOT do" section?

Add a THIRD key to your JSON object:

  "vision": {"fits": true|false, "reason": "<one short sentence>"}

Set ``fits`` false ONLY when the task clearly does not belong in the picture;
when in doubt set it true — the gate guards against obvious drift, it is not a
gatekeeper that blocks legitimate work. Always fill ``reason``.
"""

_VISION_USER_EXTRA = """
--- PROJECT VISION (knowledge/vision.md) ---
{vision}
--- END PROJECT VISION ---
"""


@dataclass
class SpecifyOutcome:
    """Result of specifying a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """Lenient JSON extraction — tolerates fenced code blocks and
    leading/trailing whitespace. Returns None if nothing parses."""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    # Greedy: find the first `{` and last `}` and try that slice.
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
    """Mirror of ``hermes_cli.kanban._profile_author``. Kept local to
    avoid a circular import when kanban.py imports this module."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "specifier"
    )


def _vision_verdict(parsed: Optional[dict]) -> Optional[tuple[bool, str]]:
    """Extract the ``(fits, reason)`` vision verdict from a parsed reply.

    Lenient by design: a missing / malformed ``vision`` key returns ``None``
    (treated as "no verdict" -> promote as usual), so an old-shape reply or a
    provider that dropped the key never strands an otherwise-fine task. Only an
    explicit ``fits: false`` holds the card.
    """
    if not isinstance(parsed, dict):
        return None
    v = parsed.get("vision")
    if not isinstance(v, dict) or "fits" not in v:
        return None
    fits = bool(v.get("fits"))
    reason = v.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else ""
    return (fits, reason)


def _record_vision_hold(task_id: str, reason: str, *, author: str) -> None:
    """Leave an audit comment explaining why the gate held a card in Triage.

    Best-effort: a failure to comment must not turn "held" into "crashed".
    """
    detail = reason or "не вписывается в текущее видение проекта"
    body = (
        "Vision-гейт: задача оставлена в Triage — не проходит "
        f"vision-проверку планировщика. Причина: {detail}. Уточните "
        "формулировку под knowledge/vision.md, либо отклоните."
    )
    try:
        with kb.connect_closing() as conn:
            kb.add_comment(conn, task_id, author, body)
    except Exception as exc:  # pragma: no cover — comment is advisory only
        logger.info("specify: could not record vision hold for %s: %s", task_id, exc)


def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SpecifyOutcome:
    """Specify a single triage task and promote it to ``todo``.

    Returns an outcome describing what happened. Never raises for expected
    failure modes (task not in triage, no aux client configured, API
    error, malformed response) — those surface via ``ok=False`` so the
    ``--all`` sweep can continue past individual failures.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return SpecifyOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover — import smoke test
        logger.debug("specify: auxiliary client import failed: %s", exc)
        return SpecifyOutcome(task_id, False, "auxiliary client unavailable")

    # Vision doc (knowledge/vision.md) — when present, the gate is handed to the
    # planner: the specifier also judges fit against the project vision and a
    # card that does not fit is held in Triage rather than auto-promoted.
    vision_text = vision_mod.load_vision_text()

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
    )
    system_prompt = _SYSTEM_PROMPT
    if vision_text:
        system_prompt += _VISION_SYSTEM_EXTRA
        user_msg += _VISION_USER_EXTRA.format(vision=vision_text)

    try:
        # Route through call_llm so auxiliary.triage_specifier.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the direct-create path dropped extra_body (#35566).
        resp = call_llm(
            task="triage_specifier",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=HERMES_KANBAN_SPECIFY_MAX_TOKENS,
            timeout=timeout or 120,
        )
    except Exception as exc:
        logger.info(
            "specify: API call failed for %s (%s) — skipping",
            task_id, exc,
        )
        return SpecifyOutcome(
            task_id, False, f"LLM error: {type(exc).__name__}"
        )

    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)

    new_title: Optional[str]
    new_body: Optional[str]
    if parsed is None:
        # Fall back: treat the whole reply as the body, leave title as-is.
        # Worst case the user edits afterward — still better than stranding
        # the task in triage on a malformed LLM reply.
        stripped_raw = raw.strip()
        if not stripped_raw:
            return SpecifyOutcome(
                task_id, False, "LLM returned an empty response"
            )
        new_title = None
        new_body = stripped_raw
    else:
        title_val = parsed.get("title")
        body_val = parsed.get("body")
        new_title = (
            title_val.strip()
            if isinstance(title_val, str) and title_val.strip()
            else None
        )
        new_body = (
            body_val if isinstance(body_val, str) and body_val.strip() else None
        )
        if new_body is None and new_title is None:
            return SpecifyOutcome(
                task_id, False, "LLM response missing title and body"
            )

    # Vision gate: an explicit ``fits: false`` verdict holds the card in Triage
    # (no auto-promotion) and records why, so obvious drift never reaches Todo
    # without a human — the nightly manual oversight this replaces.
    if vision_text:
        verdict = _vision_verdict(parsed)
        if verdict is not None and verdict[0] is False:
            reason = verdict[1]
            _record_vision_hold(
                task_id, reason, author=author or _profile_author()
            )
            return SpecifyOutcome(
                task_id, False, f"vision-check failed: {reason or 'does not fit'}"
            )

    with kb.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn,
            task_id,
            title=new_title,
            body=new_body,
            author=author or _profile_author(),
        )
    if not ok:
        # Race: someone else promoted / archived the task between our
        # read above and the write. Report, don't crash.
        return SpecifyOutcome(
            task_id, False, "task moved out of triage before promotion"
        )
    return SpecifyOutcome(task_id, True, "specified", new_title=new_title)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column.

    ``tenant`` narrows the sweep; ``None`` returns every triage task.
    """
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            include_archived=False,
        )
    return [t.id for t in tasks]
