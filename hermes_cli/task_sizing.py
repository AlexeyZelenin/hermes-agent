"""Task-size → model/effort selection rule (task t_36f2761f).

Single source of truth that maps a task's *size class* to the model role and
reasoning effort it should run at, so cheap edits stop running on the most
expensive model. Pure and I/O-free: the class is derived from the task's
title / body / category (plus an optional upstream size hint) with a keyword
heuristic — never an LLM call — and the role is turned into a concrete model
by the caller through the existing kanban model map
(:func:`hermes_cli.kanban_db.resolve_model_map`).

Design choices worth knowing:

* Only ``small`` and ``normal`` tasks *freeze* a model/effort onto the card.
  ``substantial`` freezes nothing (``model=None``, ``effort=None``) so it keeps
  inheriting the board's live default — historically the strongest model — the
  way it already did. Auto-selection therefore only ever makes a task
  *cheaper or equal*, never pins the expensive model.
* When both the ``small`` and ``substantial`` heuristics fire, ``substantial``
  wins: running a real bug-fix on the cheap model is a worse failure than
  running a cosmetic tweak on the strong one.
* A model that the task's executor cannot actually run is never frozen
  (requirement #3): we fall back to the board default and record the reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Size classes. Kept as bare strings (not an enum) so they serialise straight
# into the ``model_autoselected`` task event payload and JSON reports.
SMALL = "small"
NORMAL = "normal"
SUBSTANTIAL = "substantial"

TASK_CLASSES = (SMALL, NORMAL, SUBSTANTIAL)

# class → (model role, reasoning effort). The role is resolved to a concrete
# model by the caller via the board/config model map; the effort is one of
# ``kanban_db.VALID_EFFORT_LEVELS`` or ``None`` ("inherit the executor/board
# default"). This is the ONE table — no model tiers spelled out elsewhere.
CLASS_PLAN: dict[str, dict[str, Optional[str]]] = {
    SMALL: {"role": "cheap", "effort": "low"},
    NORMAL: {"role": "mid", "effort": "medium"},
    # substantial freezes nothing; it inherits the board default (see module
    # docstring). The role is recorded for display only.
    SUBSTANTIAL: {"role": "strong", "effort": None},
}

# Classes that actually pin a model/effort onto the card. ``substantial`` is
# deliberately absent — it inherits the live board default.
_FREEZING_CLASSES = frozenset({SMALL, NORMAL})

# Fallback model per role when the board/config model map declares none. These
# are the ONLY auto-selection model-id constants in the codebase; every model
# here MUST have a usage_pricing.py entry or the cost accounting the whole
# feature is measured by breaks. Anthropic ladder: cheap→haiku, mid→sonnet,
# strong→opus.
DEFAULT_ROLE_MODELS: dict[str, str] = {
    "cheap": "claude-haiku-4-5",
    "mid": "claude-sonnet-5",
    "strong": "claude-opus-4-8",
}

# Models each ACP executor family can actually run. A model outside its set is
# never frozen onto a task. ``hermes-worker`` is intentionally NOT listed: a
# native worker resolves and validates its own model through its provider
# stack, so any board-mapped model is fine there and needs no allowlist.
#
# [assumption] pocket Claude-Code subscriptions expose haiku/sonnet/opus via
# the CLI's advertised ``availableModels``. If a given pocket lacks one, the
# ACP client's ``session/set_model`` fallback logs it to the task log and
# continues on the agent's default model (see
# ``agent.copilot_acp_client``), so a wrong guess here degrades gracefully
# rather than failing the run.
SUPPORTED_AUTO_MODELS: dict[str, frozenset[str]] = {
    "claude-code": frozenset(
        {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}
    ),
    # codex has no verified cheap/mid ladder yet → auto-select freezes nothing
    # for it (falls back to the board default; current behaviour preserved).
    "codex": frozenset(),
}

# ── keyword heuristics ──────────────────────────────────────────────────────
# Matched case-insensitively as substrings against "title body category".
# Kept as substrings (not whole words) so Russian inflections
# ("строку"/"строке"/"строчка") and English stems ("migrat"→migrate/migration)
# all hit without a stemmer.

_SMALL_KEYWORDS = (
    # ru — cosmetics, one-liners, copy, docs
    "опечат", "переимен", "констант", "строку", "строке", "строчк",
    "стиль", "стил ", "стилей", "css", "цвет", "иконк", "отступ",
    "подпис", "лейбл", "тултип", "плейсхолдер", "формулировк", "заголовок",
    "документац", "докумен", "readme", "коммент", "выравнив", "шрифт",
    "однострочн", "мелк", "косметик", "текст кнопк", "надпис",
    # en
    "typo", "rename", "constant", "one-liner", "oneliner", "wording",
    "label", "tooltip", "placeholder", "colour", "color", "icon",
    "padding", "margin", "readme", "docs", "documentation", "comment typo",
    "cosmetic", "copy tweak", "reword", "font size",
)

_SUBSTANTIAL_KEYWORDS = (
    # ru — bugs, diagnosis, architecture, cross-file work
    "почини", "почин", "исправь баг", "баг", "ошибк", "падает", "падаю",
    "краш", "зависает", "виснет", "рефактор", "миграц", "архитектур",
    "диагностик", "расследуй", "разберись", "гонк", "дедлок", "утечк",
    "интеграц", "несколько файл", "по всему", "перепиши", "перепис",
    "оптимизир", "производительн", "деградац", "регресс",
    # en
    "fix bug", "bugfix", "crash", " hang", "regression", "refactor",
    "migrat", "architect", "diagnos", "investigate", "race condition",
    "deadlock", "memory leak", " leak", "integrat", "rewrite", "optimiz",
    "performance", "across the", "multiple files", "end-to-end", "debug",
    "root cause", "root-cause",
)

# Category-key substrings that force a class regardless of the prose scan.
# Category catalogs are board-defined, so these are matched as substrings of
# the (lowercased) category key to catch common naming ("bug", "bugfix",
# "docs", "documentation", "cosmetics", "architecture", "infra").
_SMALL_CATEGORY_HINTS = ("doc", "cosmet", "copy", "chore", "ui-copy")
_SUBSTANTIAL_CATEGORY_HINTS = (
    "bug", "architect", "infra", "migrat", "research", "diagnos", "perf",
)

# Upstream size hints (e.g. the decomposer's ``model_tier``) → class. Lets the
# decompose path reuse this module later; a size hint dominates the prose scan.
_HINT_TO_CLASS = {
    "cheap": SMALL,
    "mid": NORMAL,
    "standard": NORMAL,
    "strong": SUBSTANTIAL,
    SMALL: SMALL,
    NORMAL: NORMAL,
    SUBSTANTIAL: SUBSTANTIAL,
}


@dataclass(frozen=True)
class ModelSelection:
    """The outcome of a size-based selection.

    ``model`` / ``effort`` are what to FREEZE onto the task (``None`` = leave
    unset so the board default resolves live at dispatch). ``display_model`` is
    always populated (the frozen model, else the board default for the role)
    purely so the UI can show what the task will run on. ``supported`` is False
    when the class's model was dropped because the executor can't run it.
    """

    task_class: str
    role: str
    model: Optional[str]
    effort: Optional[str]
    display_model: Optional[str]
    reason: str
    supported: bool


def _contains_any(haystack: str, needles) -> Optional[str]:
    for n in needles:
        if n and n in haystack:
            return n
    return None


def classify_task(
    title: str,
    body: str = "",
    category: Optional[str] = None,
    *,
    size_hint: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(task_class, reason)`` for a task, without any LLM call.

    An explicit ``size_hint`` (an upstream size verdict / decomposer tier)
    dominates. Otherwise the category key and then the title+body prose are
    scanned for the substantial markers first (risk-asymmetry: mis-sizing real
    work down is worse than sizing a tweak up), then the small markers, else
    ``normal``.
    """
    hint = (size_hint or "").strip().lower()
    if hint in _HINT_TO_CLASS:
        cls = _HINT_TO_CLASS[hint]
        return cls, f"size hint {hint!r} → {cls}"

    cat = (category or "").strip().lower()
    if cat:
        if _contains_any(cat, _SUBSTANTIAL_CATEGORY_HINTS):
            return SUBSTANTIAL, f"category {cat!r} implies substantial work"
        if _contains_any(cat, _SMALL_CATEGORY_HINTS):
            return SMALL, f"category {cat!r} implies a small edit"

    text = f"{title or ''} {body or ''}".lower()
    hit = _contains_any(text, _SUBSTANTIAL_KEYWORDS)
    if hit:
        return SUBSTANTIAL, f"matched substantial marker {hit!r}"
    hit = _contains_any(text, _SMALL_KEYWORDS)
    if hit:
        return SMALL, f"matched small marker {hit!r}"
    return NORMAL, "no strong signal; defaulting to a normal single-file task"


def _model_supported(model: str, executor: Optional[str]) -> bool:
    """Whether ``model`` can actually run under ``executor``.

    Native ``hermes-worker`` validates the model through its provider stack, so
    any board-mapped model is accepted. ACP executors (claude-code / codex)
    only accept models in their advertised ladder; an unknown executor family
    accepts nothing (auto-select then freezes no model)."""
    exec_l = (executor or "hermes-worker").strip().lower()
    if exec_l == "hermes-worker":
        return True
    allowed = SUPPORTED_AUTO_MODELS.get(exec_l)
    if allowed is None:
        return False
    return model in allowed


def select_model_and_effort(
    title: str,
    body: str = "",
    category: Optional[str] = None,
    *,
    model_map: Optional[dict[str, str]] = None,
    executor: Optional[str] = None,
    size_hint: Optional[str] = None,
) -> ModelSelection:
    """Classify a task and resolve the model/effort to freeze onto it.

    ``model_map`` is the resolved role→model map for the board/project (from
    :func:`hermes_cli.kanban_db.resolve_model_map`); ``None`` falls back to the
    :data:`DEFAULT_ROLE_MODELS` ladder. ``executor`` gates the availability
    check.
    """
    model_map = model_map or {}
    task_class, class_reason = classify_task(
        title, body, category, size_hint=size_hint
    )
    plan = CLASS_PLAN[task_class]
    role = str(plan["role"])
    effort = plan["effort"]

    resolved_model = (model_map.get(role) or "").strip() or DEFAULT_ROLE_MODELS.get(role)

    # substantial: never freeze; inherit the live board default.
    if task_class not in _FREEZING_CLASSES:
        return ModelSelection(
            task_class=task_class,
            role=role,
            model=None,
            effort=None,
            display_model=resolved_model,
            reason=f"{class_reason}; inherits board default (no freeze)",
            supported=True,
        )

    if not resolved_model:
        return ModelSelection(
            task_class=task_class,
            role=role,
            model=None,
            effort=None,
            display_model=None,
            reason=f"{class_reason}; no model mapped for role {role!r} (no freeze)",
            supported=True,
        )

    if not _model_supported(resolved_model, executor):
        return ModelSelection(
            task_class=task_class,
            role=role,
            model=None,
            effort=None,
            display_model=resolved_model,
            reason=(
                f"{class_reason}; model {resolved_model!r} not supported by "
                f"executor {executor!r} — falling back to board default"
            ),
            supported=False,
        )

    return ModelSelection(
        task_class=task_class,
        role=role,
        model=resolved_model,
        effort=str(effort) if effort else None,
        display_model=resolved_model,
        reason=f"{class_reason}; frozen {resolved_model!r} @ effort {effort or 'default'}",
        supported=True,
    )
