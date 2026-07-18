"""Project vision document — resolution + loading.

The vision doc (``knowledge/vision.md``) is the materialised picture of the
project: what we build, current features, where we are heading, and what we
explicitly do NOT do. It is the single yardstick two consumers read:

* the triage->todo gate (:mod:`hermes_cli.kanban_specify`) mixes it into the
  specifier/planner context so a card that does not fit the picture is held in
  Triage instead of auto-promoted;
* the periodic backlog reconciliation (:mod:`hermes_cli.vision_reconcile`)
  measures drift — orphan cards and contradictions — against it.

Resolution order for the doc, first hit wins:

  1. ``$HERMES_VISION_DOC`` — explicit path override.
  2. ``$HERMES_PROJECT_ROOT/knowledge/vision.md``.
  3. ``<explicit project_root>/knowledge/vision.md`` when a caller passes one.
  4. ``<repo root>/knowledge/vision.md`` — the repo this code ships in.

Everything degrades to "no vision doc" (returns ``None``): a host without one
simply runs the gate/reconciler in their pre-vision behaviour rather than
erroring. Pure and side-effect-free apart from the read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

# Cap injected into an LLM context so a long doc can't blow the token budget.
DEFAULT_VISION_MAX_CHARS = 12000

_REL_PARTS = ("knowledge", "vision.md")


def _repo_root() -> Path:
    """Repo root inferred from this file: ``hermes_cli/vision.py`` -> repo."""
    return Path(__file__).resolve().parents[1]


def _candidate_roots(project_root: Optional[Union[str, Path]]) -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("HERMES_PROJECT_ROOT")
    if env_root:
        roots.append(Path(env_root).expanduser())
    if project_root is not None:
        roots.append(Path(project_root).expanduser())
    roots.append(_repo_root())
    return roots


def vision_doc_path(
    project_root: Optional[Union[str, Path]] = None,
) -> Optional[Path]:
    """Resolve the vision doc path (existence-checked), or ``None`` if absent.

    ``$HERMES_VISION_DOC`` wins outright when set and pointing at a real file;
    otherwise the first ``knowledge/vision.md`` found under the candidate roots
    (env root, explicit ``project_root``, then the shipping repo) is returned.
    """
    override = os.environ.get("HERMES_VISION_DOC")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    for root in _candidate_roots(project_root):
        candidate = root.joinpath(*_REL_PARTS)
        if candidate.is_file():
            return candidate
    return None


def load_vision_text(
    project_root: Optional[Union[str, Path]] = None,
    *,
    max_chars: int = DEFAULT_VISION_MAX_CHARS,
) -> Optional[str]:
    """Return the vision doc text (truncated to ``max_chars``), or ``None``.

    ``None`` covers every "no usable vision" case — file absent, unreadable, or
    empty — so callers can treat a truthy return as "vision available".
    """
    path = vision_doc_path(project_root)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text
