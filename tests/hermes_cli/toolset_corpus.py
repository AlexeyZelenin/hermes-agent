"""Representative task corpus + schema-size measurement for toolset selection.

Shared, non-collected helper (no ``test_`` prefix) used by both the automated
savings test (``test_toolset_savings.py``) and the report regenerator
(``scripts/measure_toolset_savings.py``). Keeping the corpus in one place means
the asserted thresholds and the published report can never drift apart.

The measurement resolves each toolset set to its concrete tool schemas via the
live tool registry and sizes them with the same ``chars/4`` estimator the
runtime uses to gate tool-search (``tools.tool_search.estimate_tokens_from_schemas``),
so the numbers are directly comparable to the deferral threshold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# A broad, representative worker CLI ceiling (an "elias-class" profile): every
# toolset the deterministic rules can target. This is the *narrowable* surface.
FULL_CEILING: list[str] = [
    "file", "terminal", "todo", "web", "browser", "vision", "image_gen",
    "cronjob", "code_execution", "delegation", "session_search", "memory",
    "skills", "clarify",
]

# The irreducible base (matches toolset_selector.DEFAULT_BASE).
BASE: list[str] = ["file", "terminal", "todo"]

# Kanban lifecycle tools are appended by model_tools for every worker regardless
# of ``--toolsets`` (HERMES_KANBAN_TASK path), so they are a constant floor the
# selector never controls. Reported separately for an honest end-to-end number.
KANBAN_FLOOR: list[str] = ["kanban"]

CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Case:
    name: str
    title: str
    body: str
    # Non-base toolsets that MUST be present (relevant selection).
    expect: tuple[str, ...] = ()
    # Toolsets that MUST NOT be present (denylist / negative signal).
    forbid: tuple[str, ...] = ()
    # True → task carries no special signal; narrows to base only.
    minimal: bool = False
    kind: str = "positive"    # positive | minimal | denylist | worst_case


# One representative task per tool group, plus negative and worst cases.
CORPUS: tuple[Case, ...] = (
    # --- relevant selection: one clear group each -------------------------
    Case("web-docs", "Search the API documentation",
         "Look up how to paginate results in the docs", expect=("web",)),
    Case("web-url", "Fetch release notes",
         "Pull data from https://example.com/releases and summarize",
         expect=("web",)),
    Case("browser", "Log into the dashboard",
         "Open https://app.example.com and click login, fill the form",
         expect=("browser",)),
    Case("vision", "Analyze the screenshot",
         "Review the attached diagram.png and describe the flow",
         expect=("vision",)),
    Case("cronjob", "Schedule a nightly job",
         "Run the sync every day at 0 2 * * *", expect=("cronjob",)),
    Case("code-exec", "Compute the totals",
         "Run this script to compute the aggregates: посчитай суммы",
         expect=("code_execution",)),
    Case("delegation-ru", "Разбей задачу",
         "Разбей на подзадачи и делегируй параллельно агентам",
         expect=("delegation",)),
    Case("session-recall", "Recall prior decision",
         "What did we decide earlier in a past session about auth",
         expect=("session_search",)),
    Case("memory-ru", "Запомни настройку",
         "Запомни мои предпочтения по стилю кода", expect=("memory",)),
    Case("skills", "Follow the deploy skill",
         "Use the deployment skill/playbook procedure", expect=("skills",)),
    Case("clarify", "Clarify scope",
         "The requirements are ambiguous, ask the user to confirm",
         expect=("clarify",)),
    # --- negative / minimal: no special signal → base only ----------------
    Case("minimal-fix", "Fix the failing test",
         "Update the assertion in test_foo and rerun", minimal=True,
         kind="minimal"),
    Case("minimal-rename", "Rename a variable",
         "Rename total to grand_total across the module", minimal=True,
         kind="minimal"),
    Case("minimal-ru", "Поправь отступы",
         "Выровняй отступы в функции parse", minimal=True, kind="minimal"),
    # --- denylist: strong signal, but capability is forbidden -------------
    Case("denylist-imggen", "Design a logo",
         "Generate an icon image for the new product",
         forbid=("image_gen",), kind="denylist"),
    Case("denylist-tts", "Narrate the intro",
         "Озвучь голосом вступление, синтез речи",
         forbid=("tts",), kind="denylist"),
    # --- worst case: a task that genuinely touches almost everything ------
    Case("worst-case", "Build and ship a feature",
         "Search the docs at https://example.com, click through the form, "
         "analyze diagram.png, schedule a cron every day, delegate subtasks, "
         "recall the past session, remember my preference, use a skill, "
         "and ask the user to confirm scope",
         kind="worst_case"),
)


@dataclass
class Row:
    case: Case
    selected: list[str]                 # final toolsets (incl. base)
    non_base: list[str]                 # selected minus base
    tokens: int                         # narrowable schema tokens
    worker_tokens: int                  # incl. kanban floor
    saved_pct: float                    # vs full-ceiling narrowable
    worker_saved_pct: float             # vs full worker surface


@dataclass
class Report:
    base_tokens: int
    ceiling_tokens: int
    ceiling_tools: int
    kanban_tokens: int
    worker_full_tokens: int
    rows: list[Row] = field(default_factory=list)
    typical_saved_pct: float = 0.0      # median over non-worst cases
    worst_saved_pct: float = 0.0        # the minimum saving in the corpus
    mean_saved_pct: float = 0.0


def schema_tokens(toolset_names: list[str]) -> tuple[int, int, int]:
    """Return ``(tool_count, bytes, est_tokens)`` for the resolved schemas.

    Imports are deferred so the module is importable without booting the whole
    tool registry (e.g. for a quick corpus inspection).
    """
    import model_tools  # noqa: F401 — side effect: registers all tools
    from tools.registry import registry
    from toolsets import resolve_multiple_toolsets

    tools = resolve_multiple_toolsets(list(toolset_names))
    chars = 0
    for name in tools:
        schema = registry.get_schema(name)
        if schema is None:
            continue
        chars += len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    return len(tools), chars, -(-chars // CHARS_PER_TOKEN)


def _selector_config(denylist: Optional[tuple[str, ...]] = None):
    from hermes_cli import toolset_selector as ts

    return ts.SelectorConfig(
        mode="narrow",
        base=tuple(BASE),
        denylist=ts.DEFAULT_DENYLIST if denylist is None else denylist,
    )


def select(case: Case) -> list[str]:
    """Run the deterministic selector for *case* against the full ceiling."""
    from hermes_cli import toolset_selector as ts

    sel = ts.select_toolsets(
        title=case.title, body=case.body,
        ceiling=list(FULL_CEILING), config=_selector_config(),
    )
    return sel.toolsets


def measure() -> Report:
    """Measure per-case and aggregate schema-size savings across the corpus."""
    import statistics

    _, _, base_tok = schema_tokens(BASE)
    ceiling_tools, _, ceiling_tok = schema_tokens(FULL_CEILING)
    _, _, kanban_tok = schema_tokens(KANBAN_FLOOR)
    _, _, worker_full_tok = schema_tokens(FULL_CEILING + KANBAN_FLOOR)

    rows: list[Row] = []
    for case in CORPUS:
        selected = select(case)
        _, _, tok = schema_tokens(selected)
        _, _, wtok = schema_tokens(selected + KANBAN_FLOOR)
        non_base = [t for t in selected if t not in BASE]
        rows.append(Row(
            case=case, selected=selected, non_base=non_base,
            tokens=tok, worker_tokens=wtok,
            saved_pct=round(100 * (ceiling_tok - tok) / ceiling_tok, 1),
            worker_saved_pct=round(100 * (worker_full_tok - wtok) / worker_full_tok, 1),
        ))

    non_worst = [r.saved_pct for r in rows if r.case.kind != "worst_case"]
    all_saves = [r.saved_pct for r in rows]
    return Report(
        base_tokens=base_tok,
        ceiling_tokens=ceiling_tok,
        ceiling_tools=ceiling_tools,
        kanban_tokens=kanban_tok,
        worker_full_tokens=worker_full_tok,
        rows=rows,
        typical_saved_pct=round(statistics.median(non_worst), 1),
        worst_saved_pct=round(min(all_saves), 1),
        mean_saved_pct=round(statistics.mean(all_saves), 1),
    )
