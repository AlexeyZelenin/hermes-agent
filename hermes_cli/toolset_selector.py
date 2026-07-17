"""Task-aware toolset selection for kanban workers.

A dispatcher-spawned ``hermes-worker`` used to receive its assignee profile's
*entire* enabled CLI tool surface, regardless of what the task actually needs.
A large tool schema costs prompt tokens and dilutes model attention.

This module narrows that surface: to a small irreducible **base set** it adds
**only the toolsets whose relevance is signalled by the task's title/body**,
while never exceeding the profile's allowlist (the ``ceiling``). A deterministic
rule engine (capability regexes + a bilingual EN/RU keyword table) does the
work; an optional auxiliary-model stage may *narrow* borderline cases further
but can never widen policy. Every path is fail-open: any error or ``mode: off``
returns the full ceiling, i.e. today's behaviour.

Pure module: the deterministic path does no I/O and no network, so the engine is
fully unit-testable. See ``docs/design/toolset-selection.md`` for the full
design and the integration seam in ``hermes_cli/kanban_db.py``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SELECTOR_VERSION = "1.0.0"

# One strong capability hit (weight 2.0) saturates confidence to 1.0; two
# independent keyword hits (weight 1.0 each) do the same.
SATURATION = 2.0
KEYWORD_WEIGHT = 1.0

DEFAULT_MODE = "off"
DEFAULT_BASE = ("file", "terminal", "todo")
DEFAULT_DENYLIST = ("computer_use", "image_gen", "spotify", "homeassistant", "tts")
DEFAULT_TAU_SELECT = 0.5
DEFAULT_TAU_LOW = 0.3
DEFAULT_AUX_TIMEOUT_S = 8.0
DEFAULT_AUX_MODEL = "auto"


# --- Rule tables (DATA, not code) ------------------------------------------
# Kept as module-level literals so weights/keywords can be tuned from real
# selection logs (§10 of the design) without touching any logic.

# Capability rules: structural, word-independent signals over ``title + body``.
# ``all`` patterns must all match; if ``any`` is present at least one must match.
# High weight (2.0) — strong, low-false-positive signals.
CAPABILITY_RULES: tuple[dict[str, Any], ...] = (
    {"id": "cap.url", "toolset": "web", "weight": 2.0, "any": (r"https?://",)},
    {
        "id": "cap.browser",
        "toolset": "browser",
        "weight": 2.0,
        "all": (r"https?://", r"\b(click|fill|log[\s-]?in|navigate|submit|form)\b"),
    },
    {
        "id": "cap.image_ref",
        "toolset": "vision",
        "weight": 2.0,
        "any": (r"!\[[^\]]*\]\([^)]*\)", r"\.(?:png|jpe?g|gif|webp|bmp|tiff?)\b"),
    },
    {
        "id": "cap.imggen",
        "toolset": "image_gen",
        "weight": 2.0,
        "all": (
            r"\b(?:generat|creat|draw|render|make)\w*\b",
            r"\b(?:image|picture|logo|icon|illustration)s?\b",
        ),
    },
    {
        "id": "cap.schedule",
        "toolset": "cronjob",
        "weight": 2.0,
        "any": (
            r"\b\d+\s+\d+\s+\*",
            r"\bevery\s+\d+\s+(?:min|minute|hour|day)",
            r"\bcron(?:tab|job)?\b",
        ),
    },
)

# Keyword rules: bilingual EN + RU stems, weight 1.0 each. Matched with a left
# word boundary + trailing ``\w*`` so RU inflection (and EN stems) are covered
# by prefix. Two independent hits on the same toolset saturate.
KEYWORD_RULES: dict[str, tuple[str, ...]] = {
    "web": (
        "search", "google", "look up", "docs", "documentation", "website",
        "поиск", "найд", "документац", "сайт", "ссылк",
    ),
    "browser": (
        "browser", "headless", "selenium", "playwright", "screenshot",
        "браузер", "скриншот",
    ),
    "vision": (
        "screenshot", "diagram", "ocr", "chart",
        "скриншот", "диаграмм", "изображени", "распозна",
    ),
    "image_gen": (
        "logo", "icon", "illustration", "render",
        "сгенерир", "логотип", "иконк", "иллюстрац",
    ),
    "code_execution": (
        "run script", "execute code", "compute", "data crunch",
        "выполни скрипт", "посчита",
    ),
    "delegation": (
        "subtask", "delegate", "fan out", "parallel agents", "sub-agent",
        "подзадач", "делегир", "параллель",
    ),
    "session_search": (
        "earlier conversation", "past session", "recall", "previously",
        "прошл", "ранее обсужда", "вспомни",
    ),
    "memory": (
        "remember", "persist", "note for later", "my preference",
        "запомни", "сохрани заметк", "предпочтени",
    ),
    "skills": (
        "skill", "playbook", "procedure",
        "скилл", "навык", "инструкци",
    ),
    "clarify": (
        "ask the user", "confirm with", "clarify", "ambiguous",
        "уточни", "спроси", "подтверд",
    ),
    "cronjob": (
        "schedule", "recurring", "cron", "periodic", "every day",
        "расписани", "регуляр", "периодич", "крон",
    ),
    "homeassistant": (
        "home assistant", "smart home", "turn on the", "thermostat",
        "умный дом", "включи свет",
    ),
    "tts": (
        "text to speech", "voice over", "narrate", "audio",
        "озвуч", "голос", "синтез речи",
    ),
}


@dataclass(frozen=True)
class SelectorConfig:
    mode: str = DEFAULT_MODE                       # "off" | "narrow"
    base: tuple[str, ...] = DEFAULT_BASE           # irreducible, always included
    denylist: tuple[str, ...] = DEFAULT_DENYLIST   # never auto-selected
    pin: tuple[str, ...] = ()                      # force-include (⊆ ceiling)
    tau_select: float = DEFAULT_TAU_SELECT         # >= this confidence → include
    tau_low: float = DEFAULT_TAU_LOW               # [tau_low, tau_select) → borderline
    aux_enabled: bool = False                      # aux chain currently dead
    aux_timeout_s: float = DEFAULT_AUX_TIMEOUT_S
    aux_model: str = DEFAULT_AUX_MODEL


@dataclass(frozen=True)
class MatchReason:
    toolset: str
    score: float
    confidence: float
    rule_ids: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class ToolsetSelection:
    toolsets: list[str]                # FINAL: deduped, sorted, ⊆ ceiling
    base: list[str] = field(default_factory=list)
    matched: list[MatchReason] = field(default_factory=list)
    borderline: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    aux_used: bool = False
    aux_delta: list[str] = field(default_factory=list)
    aux_latency_ms: Optional[float] = None
    confidence: float = 0.0
    fallback: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    selector_version: str = SELECTOR_VERSION


# --- Config loading ---------------------------------------------------------

def _as_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(v).strip() for v in value if str(v).strip())


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_selector_config(
    cfg: Optional[dict], board_overrides: Optional[dict] = None
) -> SelectorConfig:
    """Resolve :class:`SelectorConfig` from config.yaml (← optional board).

    Reads ``kanban.toolset_selection``; a per-board ``toolset_selection`` dict
    (from board metadata) overrides matching keys. Raises ``ValueError`` if a
    base toolset is also denylisted — an irreducible-vs-forbidden contradiction
    the operator must resolve.
    """
    raw: dict[str, Any] = {}
    kanban = (cfg or {}).get("kanban") if isinstance(cfg, dict) else None
    if isinstance(kanban, dict) and isinstance(kanban.get("toolset_selection"), dict):
        raw.update(kanban["toolset_selection"])
    if isinstance(board_overrides, dict):
        raw.update(board_overrides)

    aux = raw.get("aux") if isinstance(raw.get("aux"), dict) else {}
    base = _as_tuple(raw.get("base"), DEFAULT_BASE)
    denylist = _as_tuple(raw.get("denylist"), DEFAULT_DENYLIST)
    shadow = set(base) & set(denylist)
    if shadow:
        raise ValueError(
            f"toolset_selection: base toolsets {sorted(shadow)} are also denylisted"
        )

    mode = str(raw.get("mode", DEFAULT_MODE)).strip().lower() or DEFAULT_MODE
    return SelectorConfig(
        mode=mode if mode in ("off", "narrow") else DEFAULT_MODE,
        base=base,
        denylist=denylist,
        pin=_as_tuple(raw.get("pin"), ()),
        tau_select=_as_float(raw.get("tau_select"), DEFAULT_TAU_SELECT),
        tau_low=_as_float(raw.get("tau_low"), DEFAULT_TAU_LOW),
        aux_enabled=bool(aux.get("enabled", False)),
        aux_timeout_s=_as_float(aux.get("timeout_s"), DEFAULT_AUX_TIMEOUT_S),
        aux_model=str(aux.get("model", DEFAULT_AUX_MODEL) or DEFAULT_AUX_MODEL),
    )


# --- Deterministic rule engine ---------------------------------------------

def _match_capability(text: str, rule: dict[str, Any]) -> list[str]:
    """Return the matched fragments if *rule* fires, else an empty list."""
    hits: list[str] = []
    for pat in rule.get("all", ()):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            return []
        hits.append(m.group(0))
    any_pats = rule.get("any", ())
    if any_pats:
        any_hit = None
        for pat in any_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                any_hit = m.group(0)
                break
        if any_hit is None:
            return []
        hits.append(any_hit)
    return hits


def _stem_present(text: str, stem: str) -> bool:
    """Prefix match with a left word boundary; trailing ``\\w*`` for inflection."""
    return re.search(r"(?<!\w)" + re.escape(stem) + r"\w*", text, re.IGNORECASE) is not None


def _score_candidates(
    text: str, candidates: set[str]
) -> dict[str, dict[str, Any]]:
    """Accrue per-candidate score, fired rule ids, and matched signals."""
    acc: dict[str, dict[str, Any]] = {}

    def bump(ts: str, weight: float, rule_id: str, signals: list[str]) -> None:
        slot = acc.setdefault(ts, {"score": 0.0, "rules": [], "keywords": []})
        slot["score"] += weight
        if rule_id not in slot["rules"]:
            slot["rules"].append(rule_id)
        for sig in signals:
            if sig not in slot["keywords"]:
                slot["keywords"].append(sig)

    for rule in CAPABILITY_RULES:
        ts = rule["toolset"]
        if ts not in candidates:
            continue
        matched = _match_capability(text, rule)
        if matched:
            bump(ts, rule["weight"], rule["id"], matched)

    for ts, stems in KEYWORD_RULES.items():
        if ts not in candidates:
            continue
        for stem in stems:
            if _stem_present(text, stem):
                bump(ts, KEYWORD_WEIGHT, f"kw.{ts}", [stem])

    return acc


def select_toolsets(
    *, title: str, body: Optional[str], ceiling: list[str], config: SelectorConfig
) -> ToolsetSelection:
    """Select the worker's toolsets. Never raises; fails open to *ceiling*.

    Guarantees (hold on every return):
      1. ``set(result.toolsets) ⊆ set(ceiling)`` — never widen the allowlist.
      2. ``set(base) ∩ set(ceiling) ⊆ result`` — base is present if allowed.
      3. ``result.toolsets`` is deduped (casefold) and sorted.
    """
    ceiling_set = _dedupe(ceiling)
    if config.mode != "narrow":
        return ToolsetSelection(
            toolsets=sorted(ceiling_set),
            base=sorted(set(config.base) & ceiling_set),
            dropped=[],
            fallback="mode_off",
        )
    try:
        return _select_narrow(title, body, ceiling_set, config)
    except Exception as exc:  # fail-open — a selector bug degrades to today's behaviour
        _log.warning("toolset selector error (%s); using full ceiling", exc)
        return ToolsetSelection(
            toolsets=sorted(ceiling_set),
            base=sorted(set(config.base) & ceiling_set),
            fallback=f"selector_error:{type(exc).__name__}",
        )


def _dedupe(names: Any) -> set[str]:
    """Casefold-normalise names, dropping invalid/empty ones with a warning."""
    from toolsets import validate_toolset

    out: set[str] = set()
    for raw in names or []:
        name = str(raw).strip()
        if not name:
            continue
        if not validate_toolset(name):
            _log.warning("toolset selector: dropping unknown toolset name %r", name)
            continue
        out.add(name)
    return out


def _select_narrow(
    title: str, body: Optional[str], ceiling: set[str], config: SelectorConfig
) -> ToolsetSelection:
    text = f"{title or ''}\n{body or ''}"
    base = set(config.base) & ceiling
    pins = set(config.pin) & ceiling            # a pin outside the ceiling is dropped
    denylist = set(config.denylist)
    candidates = ceiling - base - pins - denylist

    acc = _score_candidates(text, candidates)
    selected: set[str] = set(base) | set(pins)
    borderline: list[str] = []
    matched: list[MatchReason] = []
    for ts in sorted(acc):
        info = acc[ts]
        confidence = min(1.0, info["score"] / SATURATION)
        matched.append(MatchReason(
            toolset=ts, score=round(info["score"], 4), confidence=round(confidence, 4),
            rule_ids=tuple(info["rules"]), keywords=tuple(info["keywords"]),
        ))
        if confidence >= config.tau_select:
            selected.add(ts)
        elif confidence >= config.tau_low:
            borderline.append(ts)

    aux_used, aux_delta, aux_latency = False, [], None
    if config.aux_enabled and borderline:
        promoted, aux_used, aux_latency = _aux_rerank(text, borderline, config)
        for ts in promoted:
            if ts in candidates:               # narrow-only: aux ⊆ borderline ⊆ candidates
                selected.add(ts)
                aux_delta.append(ts)

    final = sorted(selected & ceiling)          # invariant 1, belt-and-braces
    non_base = [m.confidence for m in matched if m.toolset in selected and m.toolset not in base]
    return ToolsetSelection(
        toolsets=final,
        base=sorted(base),
        matched=matched,
        borderline=sorted(borderline),
        dropped=sorted(ceiling - set(final)),
        aux_used=aux_used,
        aux_delta=sorted(aux_delta),
        aux_latency_ms=aux_latency,
        confidence=round(sum(non_base) / len(non_base), 4) if non_base else 0.0,
        fallback=None,
    )


# --- Optional aux re-ranking (skippable, narrow-only) ----------------------

def _aux_rerank(
    text: str, borderline: list[str], config: SelectorConfig
) -> tuple[list[str], bool, Optional[float]]:
    """Ask the aux model which borderline toolsets to keep. Narrow-only.

    Returns ``(promoted, used, latency_ms)``. Any failure (disabled backend,
    timeout, HTTP error, malformed output) → ``([], False, None)`` and the
    deterministic result stands. The narrow-only guarantee is enforced by the
    caller (intersection with ``candidates``); this function only proposes.
    """
    try:
        from agent.auxiliary_client import call_llm
        from toolsets import TOOLSETS

        catalogue = {
            ts: (TOOLSETS.get(ts, {}) or {}).get("description", ts) for ts in borderline
        }
        prompt = (
            "You narrow a coding agent's toolset. Given the task and a list of "
            "candidate toolsets, return STRICT JSON {\"include\": [names]} listing "
            "ONLY the candidates the task clearly needs. Include nothing else.\n\n"
            f"Task:\n{text[:4000]}\n\nCandidates:\n"
            + "\n".join(f"- {n}: {d}" for n, d in catalogue.items())
        )
        resp = call_llm(
            task="toolset_select",
            model=config.aux_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            timeout=config.aux_timeout_s,
        )
        content = resp.choices[0].message.content or ""
        promoted = _parse_aux_include(content, set(borderline))
        return promoted, True, None
    except Exception as exc:
        _log.info("toolset selector: aux re-rank skipped (%s)", exc)
        return [], False, None


def _parse_aux_include(content: str, allowed: set[str]) -> list[str]:
    """Extract ``include`` from the model's JSON, keeping only *allowed* names."""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return []
    parsed = json.loads(content[start : end + 1])
    include = parsed.get("include") if isinstance(parsed, dict) else None
    if not isinstance(include, list):
        return []
    return [str(x).strip() for x in include if str(x).strip() in allowed]


# --- Observability ----------------------------------------------------------

def emit_selection_log(task: Any, board: Any, selection: ToolsetSelection) -> None:
    """Emit one structured ``toolset_selection`` record. Never raises."""
    try:
        record = {
            "event": "toolset_selection",
            "schema_version": selection.schema_version,
            "selector_version": selection.selector_version,
            "task_id": getattr(task, "id", None),
            "run_id": getattr(task, "current_run_id", None),
            "board": board,
            "profile": getattr(task, "assignee", None),
            "executor": getattr(task, "executor", None),
            "selected": selection.toolsets,
            "base": selection.base,
            "dropped": selection.dropped,
            "borderline": selection.borderline,
            "matched": [
                {
                    "toolset": m.toolset, "score": m.score, "confidence": m.confidence,
                    "rule_ids": list(m.rule_ids), "keywords": list(m.keywords),
                }
                for m in selection.matched
            ],
            "aux_used": selection.aux_used,
            "aux_delta": selection.aux_delta,
            "aux_latency_ms": selection.aux_latency_ms,
            "confidence": selection.confidence,
            "fallback": selection.fallback,
        }
        # WARNING only for real error fallbacks (a selector bug degraded to the
        # full ceiling). ``mode_off`` is the default steady state and would
        # otherwise flood WARNING on every spawn, so it logs at DEBUG; a normal
        # narrow selection logs at INFO.
        fb = selection.fallback
        if fb and fb.startswith("selector_error"):
            level = logging.WARNING
        elif fb == "mode_off":
            level = logging.DEBUG
        else:
            level = logging.INFO
        _log.log(level, "toolset_selection %s", json.dumps(record, ensure_ascii=False))
    except Exception as exc:
        _log.debug("toolset selector: could not emit selection log (%s)", exc)
