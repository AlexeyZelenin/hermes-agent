"""Context handoff policy for dispatcher-spawned Kanban workers.

Workers benefit from a fresh model session before long-lived context starts to
degrade.  The policy deliberately uses an absolute prompt-token threshold rather
than a percentage of whichever context window a provider advertises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

DEFAULT_HANDOFF_TOKENS = 100_000
HANDOFF_RELATIVE_PATH = Path(".hermes") / "kanban-handoff.md"


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_handoff_threshold(
    config: Mapping[str, Any] | None,
    model: str | None,
    context_length: int,
) -> int:
    """Return the safe absolute handoff threshold for one Kanban worker.

    ``context_handoff_tokens_by_model`` is intentionally a simple model-name
    mapping so operators can tune a provider/model without changing the global
    policy.  A model with a smaller context window keeps 20% headroom rather
    than receiving an unusable threshold at or above its maximum window.
    """
    config = config if isinstance(config, Mapping) else {}
    threshold = _positive_int(config.get("context_handoff_tokens"), DEFAULT_HANDOFF_TOKENS)
    overrides = config.get("context_handoff_tokens_by_model")
    if isinstance(overrides, Mapping) and model:
        override = overrides.get(model)
        if override is None:
            override = overrides.get(str(model).lower())
        if override is not None:
            threshold = _positive_int(override, threshold)

    if context_length > 0:
        threshold = min(threshold, max(1, int(context_length * 0.80)))
    return threshold


def write_handoff_checkpoint(
    *,
    workspace: str | Path,
    task_id: str,
    model: str,
    prompt_tokens: int,
    summary: str,
) -> Path:
    """Persist the compression handoff where the next worker can read it."""
    path = Path(workspace).expanduser().resolve() / HANDOFF_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Kanban worker handoff\n\n"
        f"Task: `{task_id}`\n"
        f"Model: `{model}`\n"
        f"Prompt tokens at handoff: {max(0, int(prompt_tokens)):,}\n\n"
        "## Compacted session state\n\n"
        f"{summary.strip()}\n",
        encoding="utf-8",
    )
    return path
