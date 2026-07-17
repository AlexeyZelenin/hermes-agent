"""Git-backed project knowledge-vault helpers.

The project vault is canonical Markdown.  This module deliberately does not
write into agent memory: downstream indexing can be rebuilt from these files.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class DecisionValidationError(ValueError):
    """The worker supplied no usable structured decision."""


class DecisionRegistryError(RuntimeError):
    """The configured project registry cannot safely be updated."""


def _required_text(decision: dict[str, Any], field: str) -> str:
    value = decision.get(field)
    text = str(value).strip() if isinstance(value, str) else ""
    if not text:
        raise DecisionValidationError(f"decision.{field} must be a non-empty string")
    return text


def _normalise_links(decision: dict[str, Any]) -> list[str]:
    raw = decision.get("links", [])
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise DecisionValidationError("decision.links must be a list of non-empty strings")
    links: list[str] = []
    for raw_link in raw:
        link = str(raw_link).strip() if isinstance(raw_link, str) else ""
        if not link:
            raise DecisionValidationError("decision.links must contain only non-empty strings")
        if link not in links:
            links.append(link)
    return links


def _registry_path(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    registry = root / "knowledge" / "decisions.md"
    if not registry.is_file():
        raise DecisionRegistryError(
            f"project decision registry is missing: {registry}; "
            "create knowledge/decisions.md in the project repository first"
        )
    return registry


def _require_git_backing(registry: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(registry.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DecisionRegistryError(
            f"could not verify Git backing for decision registry {registry}: {exc}"
        ) from exc
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise DecisionRegistryError(
            f"project decision registry must be inside a Git worktree: {registry}"
        )


def _markdown_entry(*, board: str, task_id: str, summary: str, rationale: str, links: list[str]) -> str:
    lines = [
        "",
        f"## {task_id} — {summary}",
        "",
        f"- Task: `kanban:{board}/{task_id}`",
        f"- Rationale: {rationale}",
    ]
    lines.extend(f"- Related: `{link}`" for link in links)
    lines.extend(["", f"<!-- kanban-decision:{task_id} -->", ""])
    return "\n".join(lines)


def record_task_decision(
    *,
    project_root: str | Path,
    board: str,
    task_id: str,
    decision: dict[str, Any],
) -> Path:
    """Append one deduplicated structured decision to a project's registry.

    The stable HTML marker provides idempotency across retries.  The registry is
    verified to be inside Git before its contents are mutated.
    """
    if not isinstance(decision, dict):
        raise DecisionValidationError("decision must be an object")
    summary = _required_text(decision, "summary")
    rationale = _required_text(decision, "rationale")
    links = _normalise_links(decision)
    task_id = str(task_id).strip()
    board = str(board).strip()
    if not task_id or not board:
        raise DecisionValidationError("task_id and board must be non-empty")

    registry = _registry_path(project_root)
    _require_git_backing(registry)
    marker = f"<!-- kanban-decision:{task_id} -->"
    entry = _markdown_entry(
        board=board, task_id=task_id, summary=summary,
        rationale=rationale, links=links,
    )

    # Locking prevents concurrent completions from racing the marker check.
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback is still safe enough for retry dedupe.
        fcntl = None
    with registry.open("r+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            contents = handle.read()
            if marker in contents:
                return registry
            handle.seek(0, os.SEEK_END)
            if contents and not contents.endswith("\n"):
                handle.write("\n")
            handle.write(entry)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return registry
