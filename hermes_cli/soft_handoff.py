"""Soft context-budget handoff policy for the kanban goal loop.

When a goal-mode worker's live context window crosses a soft occupancy
threshold (~60%), continuing to pile turns onto the same session risks
repeated in-session compaction (which degrades summary quality) and, past
that, the hard context-length wall. Rather than ride the same session down,
the worker performs a *soft handoff*: it writes its task state to a small,
versioned spec-file and resumes in a FRESH session with a clean window that
picks up from that spec.

This is deliberately a pre-emptive, LOW-risk mechanism layered ABOVE the
existing hard defenses (auto-compaction and the goal loop's turn budget) and
never a replacement for them - the hard limits stay the last line and are not
touched here. Mirroring :mod:`agent.budget_guard`: :func:`evaluate` is
side-effect free (the caller performs the spec write / session reset), every
failure and missing-metric path falls back to "just continue in the same
session", and a cyclic-handoff cap plus the outer turn budget bound any thrash.

Observability: a dedicated ``hermes.kanban.soft_handoff`` logger emits one
structured JSON record per decision (threshold reached, handoff performed,
error, and chosen fallback) so an operator can see exactly why a worker did or
did not hand off.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)
# Structured metric stream: one JSON line per handoff decision/event.
metric_logger = logging.getLogger("hermes.kanban.soft_handoff")

DEFAULT_SOFT_PCT = 0.60
DEFAULT_MAX_HANDOFFS = 2
SPEC_SCHEMA_VERSION = 1
SPEC_KIND = "soft-handoff-spec"

ACTION_HANDOFF = "handoff"
ACTION_CONTINUE = "continue"

# Decision reason codes (also the metric ``code`` field).
CODE_THRESHOLD_REACHED = "threshold_reached"
CODE_BELOW_THRESHOLD = "below_threshold"
CODE_DISABLED = "disabled"
CODE_NO_METRICS = "no_metrics"
CODE_COMPACTING = "compacting"
CODE_HANDOFF_CAP = "handoff_cap"


@dataclass(frozen=True)
class SoftHandoffConfig:
    """Threshold and cap for the soft context-budget handoff.

    ``enabled`` defaults to ``True``: unlike a hard circuit-breaker, a soft
    handoff only starts a fresh session (recoverable, bounded by
    ``max_handoffs`` and the outer turn budget), so it is safe to ship on by
    default with an operator kill-switch (``HERMES_KANBAN_SOFT_HANDOFF=0``).
    """

    enabled: bool = True
    soft_pct: float = DEFAULT_SOFT_PCT
    max_handoffs: int = DEFAULT_MAX_HANDOFFS
    schema_version: int = SPEC_SCHEMA_VERSION

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SoftHandoffConfig":
        env = env if env is not None else os.environ
        defaults = cls()
        return cls(
            enabled=_as_bool(env.get("HERMES_KANBAN_SOFT_HANDOFF"), defaults.enabled),
            soft_pct=_as_pct(env.get("HERMES_KANBAN_SOFT_HANDOFF_PCT"), defaults.soft_pct),
            max_handoffs=_as_int(
                env.get("HERMES_KANBAN_SOFT_HANDOFF_MAX"), defaults.max_handoffs
            ),
        )


@dataclass(frozen=True)
class HandoffDecision:
    """Verdict from :func:`evaluate`; ``code`` explains the branch taken."""

    action: str
    code: str
    occupancy: Optional[float]
    detail: str = ""

    @property
    def should_handoff(self) -> bool:
        return self.action == ACTION_HANDOFF


@dataclass(frozen=True)
class HandoffOutcome:
    """Result of :func:`maybe_handoff`: the prompt to run next and whether a
    handoff actually happened (session reset + spec written)."""

    prompt: str
    handed_off: bool
    decision: HandoffDecision
    spec_path: Optional[str] = None


def evaluate(
    occupancy: Optional[float],
    *,
    compaction_active: bool,
    handoffs_done: int,
    config: SoftHandoffConfig,
) -> HandoffDecision:
    """Decide whether to hand off, given live context occupancy.

    Ordering encodes the policy: a disabled guard, unknown metrics, sub-
    threshold occupancy, an in-flight compaction, and an exhausted handoff cap
    all resolve to CONTINUE (never wedge; defer to the hard defenses). Only a
    known occupancy at/above the soft threshold, with headroom left under the
    cap and no compaction already running, triggers a handoff.
    """
    if not config.enabled:
        return HandoffDecision(ACTION_CONTINUE, CODE_DISABLED, occupancy, "soft handoff disabled")
    if occupancy is None:
        return HandoffDecision(
            ACTION_CONTINUE, CODE_NO_METRICS, None,
            "context occupancy unavailable; deferring to hard defenses",
        )
    if occupancy < config.soft_pct:
        return HandoffDecision(ACTION_CONTINUE, CODE_BELOW_THRESHOLD, occupancy)
    if compaction_active:
        return HandoffDecision(
            ACTION_CONTINUE, CODE_COMPACTING, occupancy,
            "auto-compaction already in progress; not double-acting",
        )
    if handoffs_done >= config.max_handoffs:
        return HandoffDecision(
            ACTION_CONTINUE, CODE_HANDOFF_CAP, occupancy,
            f"soft-handoff cap reached ({handoffs_done}/{config.max_handoffs}); "
            "falling back to in-session turns and the hard limits",
        )
    return HandoffDecision(
        ACTION_HANDOFF, CODE_THRESHOLD_REACHED, occupancy,
        f"context occupancy {occupancy:.0%} >= soft threshold {config.soft_pct:.0%}",
    )


def build_spec(
    *,
    task_id: str,
    goal_text: str,
    progress: str,
    next_step: str,
    handoff_index: int,
    source_session_id: Optional[str],
    occupancy: Optional[float],
    created_at: str,
    decisions: Optional[Sequence[str]] = None,
    schema_version: int = SPEC_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Build the versioned handoff spec - a self-sufficient artifact the fresh
    session reads. The schema is a stable contract (task ref / progress /
    decisions-so-far / next-step); ``schema_version`` lets a future v2 stay
    backward-readable."""
    return {
        "schema_version": int(schema_version),
        "kind": SPEC_KIND,
        "task_id": task_id or "",
        "goal": goal_text or "",
        "progress": progress or "",
        "decisions": [str(d) for d in (decisions or [])],
        "next_step": next_step or "",
        "handoff_index": int(handoff_index),
        "source_session_id": source_session_id or "",
        "occupancy_at_handoff": (
            round(float(occupancy), 4) if occupancy is not None else None
        ),
        "created_at": created_at,
    }


def spec_path(spec_dir: Any, task_id: str, handoff_index: int) -> Path:
    """Deterministic per-(task, handoff) spec path so a re-run overwrites the
    same file rather than accumulating duplicates."""
    return Path(spec_dir) / f"{_safe_name(task_id)}.handoff-{int(handoff_index)}.json"


def write_spec_file(path: Any, spec: Mapping[str, Any]) -> bool:
    """Idempotently persist ``spec`` to ``path`` (atomic replace).

    Returns ``True`` on success, ``False`` on any I/O failure - the caller
    treats a failed write as a fallback-to-continue, never a crash. Writing the
    same state to the same path twice is a no-op (byte-identical content is not
    rewritten)."""
    path = Path(path)
    payload = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return True
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("soft handoff: failed to write spec %s: %s", path, exc)
        return False


def render_continuation_prompt(path: Any, spec: Mapping[str, Any]) -> str:
    """The first message the FRESH session receives. It re-establishes full
    task context (the new session has empty history) by embedding the state
    inline AND pointing at the durable spec file for the exact contract."""
    decisions = spec.get("decisions") or []
    decisions_block = (
        "\n".join(f"  - {d}" for d in decisions) if decisions else "  (none recorded)"
    )
    return (
        "SOFT CONTEXT HANDOFF - you are a FRESH session resuming an in-progress "
        "kanban task. The previous session's context window filled past the soft "
        "threshold, so its state was checkpointed and handed to you with a clean "
        "window.\n\n"
        f"Full spec (schema v{spec.get('schema_version')}) saved at:\n  {path}\n\n"
        f"TASK: {spec.get('task_id')}\n"
        f"GOAL:\n{_truncate(spec.get('goal'), 1200)}\n\n"
        f"PROGRESS SO FAR:\n{_truncate(spec.get('progress'), 4000)}\n\n"
        f"DECISIONS SO FAR:\n{decisions_block}\n\n"
        f"NEXT STEP:\n{_truncate(spec.get('next_step'), 1200)}\n\n"
        "Continue from here. Do NOT redo work already completed. Finish by "
        "calling kanban_complete (or kanban_block if genuinely stuck)."
    )


def emit_metric(event: str, **fields: Any) -> None:
    """Emit one structured JSON metric line. Never raises."""
    try:
        payload = json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True)
        metric_logger.info("soft_handoff %s", payload)
    except Exception:  # pragma: no cover - telemetry must never break the loop
        pass


def maybe_handoff(
    *,
    base_prompt: str,
    task_id: str,
    goal_text: str,
    progress: str,
    next_step: str,
    handoffs_done: int,
    config: SoftHandoffConfig,
    occupancy_fn: Optional[Callable[[], Optional[float]]],
    compaction_fn: Optional[Callable[[], bool]] = None,
    reset_fn: Optional[Callable[[], Any]] = None,
    session_id_fn: Optional[Callable[[], Optional[str]]] = None,
    spec_dir: Any = None,
    now_fn: Optional[Callable[[], str]] = None,
    decisions: Optional[Sequence[str]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> HandoffOutcome:
    """Orchestrate one soft-handoff decision for the goal loop.

    Reads live occupancy, evaluates the policy, and - only on a HANDOFF verdict
    - writes the spec, resets to a fresh session, and returns the continuation
    prompt for that session. Any missing dependency or failure (no occupancy,
    no reset callable, spec write fails, reset raises) degrades to
    ``HandoffOutcome(base_prompt, handed_off=False, ...)`` so the caller simply
    runs the next turn in the same session. Never raises.
    """
    occupancy = _safe_call(occupancy_fn)
    compaction_active = bool(_safe_call(compaction_fn)) if compaction_fn else False
    decision = evaluate(
        occupancy,
        compaction_active=compaction_active,
        handoffs_done=handoffs_done,
        config=config,
    )
    emit_metric(
        "evaluate", task=task_id, code=decision.code, action=decision.action,
        occupancy=occupancy, handoffs_done=handoffs_done,
        soft_pct=config.soft_pct, max_handoffs=config.max_handoffs,
    )
    if not decision.should_handoff:
        return HandoffOutcome(base_prompt, False, decision)

    # From here we intend to hand off; any snag falls back to CONTINUE.
    if reset_fn is None or spec_dir is None:
        emit_metric("fallback", task=task_id, reason="missing_reset_or_spec_dir",
                    occupancy=occupancy)
        return HandoffOutcome(base_prompt, False, decision)

    index = handoffs_done + 1
    created_at = _safe_call(now_fn) or ""
    source_session = _safe_call(session_id_fn)
    spec = build_spec(
        task_id=task_id, goal_text=goal_text, progress=progress, next_step=next_step,
        handoff_index=index, source_session_id=source_session, occupancy=occupancy,
        created_at=created_at, decisions=decisions, schema_version=config.schema_version,
    )
    path = spec_path(spec_dir, task_id, index)
    if not write_spec_file(path, spec):
        emit_metric("fallback", task=task_id, reason="spec_write_failed",
                    spec=str(path), occupancy=occupancy)
        _log(log, f"soft handoff: spec write failed at {path}; continuing in-session")
        return HandoffOutcome(base_prompt, False, decision)

    try:
        reset_fn()
    except Exception as exc:
        emit_metric("fallback", task=task_id, reason="reset_failed",
                    error=type(exc).__name__, occupancy=occupancy)
        _log(log, f"soft handoff: session reset failed ({exc}); continuing in-session")
        return HandoffOutcome(base_prompt, False, decision)

    prompt = render_continuation_prompt(path, spec)
    emit_metric(
        "handoff", task=task_id, index=index, occupancy=occupancy,
        soft_pct=config.soft_pct, spec=str(path), source_session=source_session or "",
    )
    _log(log, f"soft handoff #{index} for {task_id}: occupancy {occupancy:.0%} >= "
              f"{config.soft_pct:.0%}; fresh session resuming from {path}")
    return HandoffOutcome(prompt, True, decision, spec_path=str(path))


def _log(log: Optional[Callable[[str], None]], msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _safe_call(fn: Optional[Callable[[], Any]]) -> Any:
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def _safe_name(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name or "task"))


def _truncate(text: Any, limit: int) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _as_pct(value: Any, default: float) -> float:
    """Parse a threshold as a fraction in (0, 1). Accepts ``0.6`` or ``60``.
    Out-of-range or unparseable values fall back to ``default``."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed > 1.0:
        parsed = parsed / 100.0
    return parsed if 0.0 < parsed < 1.0 else default
