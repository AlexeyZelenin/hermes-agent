# Design: task-aware toolset selection for kanban workers

Task: `t_8ab9bbbc` — «Спроектировать механизм подбора тулов по описанию задачи».
Operator decision (2026-07-17, decision #4): **approve the RECOMMENDED option —
deterministic rule-based toolset selector with optional aux re-ranking for
borderline cases.** Aux only refines; it can never widen policy. The aux chain
is currently dead (OpenRouter), so the aux stage must be cleanly skippable.
Implementation lands under epic `t_5d8702b9`; tests under `t_44c4a030`.

TL;DR (RU): к минимальному базовому набору тулсетов детерминированный движок
правил (эвристики возможностей + ключевые слова) добавляет только тулсеты,
релевантные title/body задачи, оставаясь ⊆ разрешённого профилем набора.
Пограничные случаи может уточнить опциональная aux-модель (только сузить, не
расширить). Любая ошибка → fail-open к текущему полному набору. Всё логируется:
какие тулсеты, почему, что отброшено.

---

## 1. Goal & scope

Today a dispatcher-spawned **hermes-worker** gets the assignee profile's *entire*
enabled CLI tool surface, regardless of what the task actually needs
(`_resolve_worker_cli_toolsets` → `_get_platform_tools(cfg,"cli")`, pinned as
`--toolsets`; `hermes_cli/kanban_db.py:8517-8548, 8698-8700`). A large tool
schema costs prompt tokens and dilutes model attention.

This mechanism replaces "give the worker everything the profile allows" with
"give the worker a small **base set** plus **only the toolsets relevant to the
task's title/body**", while never exceeding the profile's allowlist.

**In scope:** the native `hermes-worker` executor path (the only one that
consumes `--toolsets`).

**Out of scope:**
- `claude-code` / `codex` executors — they swap to `agent.acp_task_executor`
  and bring their own harness tools; `--toolsets` is not consumed
  (`kanban_db.py:8715-8721`). The selector **bypasses** them (returns unchanged).
- The task-scoped kanban lifecycle tools (`kanban_complete`, `kanban_block`, …)
  — injected separately by `model_tools` when `HERMES_KANBAN_TASK` is set
  (`kanban_db.py:8525-8526`). Orthogonal; the selector never touches them.
- Tool-level dedup — already handled: `resolve_toolset()` unions tools into a
  set (`toolsets.py:757-766`).
- Per-tool environment availability — already handled by `check_fn`/
  `requires_env` gating downstream (`tools/registry.py:96-104`). The selector
  works at the **toolset** granularity and does not re-check env.

---

## 2. Architecture & integration seam

New pure module: **`hermes_cli/toolset_selector.py`** (no I/O, no network on the
deterministic path; fully unit-testable).

Single call site: **`hermes_cli/kanban_db.py::_resolve_worker_cli_toolsets`**.
Change its signature from `(hermes_home)` to `(hermes_home, task, board)` — the
caller `_default_spawn` already has `task` and `board` in scope
(`kanban_db.py:8551-8556, 8698`). After resolving the profile ceiling:

```python
# hermes_cli/kanban_db.py, inside _resolve_worker_cli_toolsets
ceiling = sorted(_get_platform_tools(cfg, "cli"))     # unchanged: the allowlist
if task.executor not in ("hermes-worker", None):      # ACP harnesses bypass
    return ceiling or None
selection = select_toolsets(
    title=task.title, body=task.body,
    ceiling=ceiling, config=load_selector_config(cfg),
)
emit_selection_log(task, board, selection)            # observability (§9)
return selection.toolsets or ceiling or None          # fail-open guard (§8)
```

No other call site changes. `--toolsets` continues to be passed exactly as today
(`kanban_db.py:8698-8700`); only its *contents* narrow.

**Data flow:**
`_default_spawn(task)` → `_resolve_worker_cli_toolsets(home, task, board)` →
`toolset_selector.select_toolsets()` → deterministic rule engine → (optional)
`agent.auxiliary_client.call_llm()` for borderline → `ToolsetSelection` →
`--toolsets <csv>` → worker's `run_agent` resolves via `resolve_toolset()`.

---

## 3. Input / output contract

```python
@dataclass(frozen=True)
class SelectorConfig:
    mode: str                 # "off" | "narrow"   (default "off")
    base: tuple[str, ...]     # irreducible toolsets, always included
    denylist: tuple[str, ...] # never auto-selected (even on keyword match)
    tau_select: float         # >= this confidence → include        (default 0.50)
    tau_low: float            # [tau_low, tau_select) → borderline   (default 0.30)
    aux_enabled: bool         # default False (aux chain currently dead)
    aux_timeout_s: float      # default 8.0
    aux_model: str            # default "auto"

@dataclass(frozen=True)
class MatchReason:
    toolset: str
    score: float              # raw summed rule weight
    confidence: float         # min(1.0, score / SATURATION)
    rule_ids: tuple[str, ...] # which rules fired (audit)
    keywords: tuple[str, ...] # which terms/signals matched (audit)

@dataclass(frozen=True)
class ToolsetSelection:
    toolsets: list[str]       # FINAL: deduped, sorted, ⊆ ceiling. Feeds --toolsets
    base: list[str]           # base toolsets included
    matched: list[MatchReason]
    borderline: list[str]     # scored in [tau_low, tau_select)
    dropped: list[str]        # ceiling − toolsets (what was withheld, and why via matched)
    aux_used: bool
    aux_delta: list[str]      # toolsets aux added/removed vs deterministic result
    aux_latency_ms: float | None
    confidence: float         # aggregate mean confidence of selected non-base
    fallback: str | None      # None, or "selector_error:<Cls>" / "mode_off"
    schema_version: int       # = SCHEMA_VERSION
    selector_version: str

def select_toolsets(
    *, title: str, body: str, ceiling: list[str], config: SelectorConfig,
) -> ToolsetSelection: ...
```

**Hard invariants (must hold on every return):**
1. `set(selection.toolsets) ⊆ set(ceiling)` — never widen beyond the profile
   allowlist. (Enforced by intersection at the end, not by trusting rules/aux.)
2. `set(config.base) ∩ set(ceiling) ⊆ set(selection.toolsets)` — base is always
   present *if the profile allows it*. Base cannot pull in a toolset the profile
   disabled (invariant 1 wins).
3. `selection.toolsets` is deduped (casefold-normalized) and sorted.
4. On any internal error, invariant 1 still holds via fail-open to `ceiling`.

`mode == "off"` short-circuits: returns `ToolsetSelection(toolsets=ceiling, …,
fallback="mode_off")` — behaviorally identical to today. Ships dark; enabled
per-board.

---

## 4. Minimal base set

`base` is the irreducible coding core, always included (subject to invariant 1):

**Default `base = ("file", "terminal", "todo")`.**

Rationale: every code task reads/writes files, runs commands, and benefits from a
plan scratchpad. These mirror the always-on core of the `coding` posture
(`toolsets.py:346-367`). Everything else is earned by task content.

Config error (fail at load, not at runtime): if `set(base) ∩ set(denylist) ≠ ∅`,
raise — base is irreducible and a denylist entry that shadows base is a
contradiction the operator must resolve.

`skills`, `memory`, `session_search`, `clarify` are deliberately **not** base by
default (keeps the set minimal per the task's intent). They are reachable via
keyword rules (§5) or promotable to `base` per-profile if an operator wants them
always-on.

---

## 5. Deterministic rule engine

Candidate set = `(ceiling − base − denylist)`. Base and denylisted toolsets are
never scored (base is in unconditionally; denylist is out unconditionally).

Each candidate toolset accrues a **score** = sum of fired rule weights. Two rule
kinds:

### 5a. Capability rules (structural, word-independent)
Regex/structural signals over `title + "\n" + body`. High weight because they are
strong, low-false-positive signals.

| id             | signal (regex/structural)                            | toolset(s)        | weight |
|----------------|------------------------------------------------------|-------------------|--------|
| `cap.url`      | `https?://` present                                  | `web`             | 2.0    |
| `cap.browser`  | url + verb {click, fill, login, navigate, form}      | `browser`         | 2.0    |
| `cap.image_ref`| markdown image `![…](…)` or attachment/`.png/.jpg`   | `vision`          | 2.0    |
| `cap.imggen`   | {generate,create,draw,render} + {image,picture,logo} | `image_gen`       | 2.0    |
| `cap.schedule` | cron expr `\d+ \d+ \* …` or "every N min/hours/daily" | `cronjob`         | 2.0    |

`SATURATION = 2.0`: one strong capability hit → confidence 1.0.

### 5b. Keyword rules (bilingual EN + RU, word-boundary, case-insensitive)
Weight 1.0 each; two independent keyword hits saturate. Match on stems with
word boundaries (`\bpoisk`, RU handled by prefix match to cover inflection).

| toolset          | EN stems                                              | RU stems                              |
|------------------|-------------------------------------------------------|---------------------------------------|
| `web` / `search` | search, google, look up, docs, documentation, website | поиск, найд, документац, сайт, ссылк   |
| `browser`        | browser, headless, selenium, playwright, screenshot   | браузер, скриншот                     |
| `vision`         | screenshot, diagram, image analysis, ocr, chart       | скриншот, диаграмм, изображени, распозна |
| `image_gen`      | generate image, logo, icon, illustration, render      | сгенерир, логотип, иконк, иллюстрац   |
| `code_execution` | run script, execute code, compute, data crunch        | выполни скрипт, посчита               |
| `delegation`     | subtask, delegate, fan out, parallel agents, sub-agent | подзадач, делегир, параллель          |
| `session_search` | earlier conversation, past session, recall, previously | прошл, ранее обсужда, вспомни         |
| `memory`         | remember, persist, note for later, my preference      | запомни, сохрани заметк, предпочтени  |
| `skills`         | skill, playbook, use the skill, procedure             | скилл, навык, инструкци               |
| `clarify`        | ask the user, confirm with, clarify, ambiguous        | уточни, спроси, подтверд              |
| `cronjob`        | schedule, recurring, cron, periodic, every day        | расписани, регуляр, периодич, крон    |
| `homeassistant`  | home assistant, smart home, turn on the, thermostat   | умный дом, включи свет                |
| `tts`            | text to speech, voice over, narrate, audio            | озвуч, голос, синтез речи             |

Toolsets in `ceiling` with **no rule** and not in `base` score 0 → dropped under
`narrow`. That is the intended minimization: unknown-relevance = excluded.

The table is **data**, not code (a module-level dict), so tuning weights/keywords
from real logs (§9) needs no logic change.

### 5c. Scoring → decision
For each candidate: `confidence = min(1.0, score / SATURATION)`.
- `confidence ≥ tau_select` (0.50) → **select** (deterministic).
- `tau_low ≤ confidence < tau_select` (0.30–0.50) → **borderline** → §6.
- `confidence < tau_low` → **drop**.

A single keyword hit (score 1.0 → conf 0.5) clears `tau_select`: one clear signal
is enough. Weaker fuzzy matches (if partial-weight rules are added later, e.g.
0.6) land in the borderline band for aux to judge.

---

## 6. Optional aux re-ranking (skippable, narrow-only)

Invoked **only when all hold**: `config.aux_enabled` AND `borderline` non-empty
AND `auxiliary_client` resolves a backend. Otherwise skipped entirely — the
deterministic result stands and borderline toolsets are **excluded** (bias to
minimal).

Call: `agent.auxiliary_client.call_llm(task="toolset_select", model=config.aux_model,
messages=[…], max_tokens=…, timeout=config.aux_timeout_s)`
(`agent/auxiliary_client.py:6665`). Prompt gives the task title/body + the
**borderline toolsets only**, each with its `TOOLSETS[name]["description"]`, and
asks for strict JSON: `{"include": ["<toolset>", …], "reason": {…}}`.

**Narrow-only guarantee (enforced in code, not trusted from the model):**
`aux_pick = set(parsed["include"]) ∩ set(borderline)`. Anything the aux returns
outside the borderline set is discarded. Aux can only *promote a borderline
candidate into the result* — it cannot add a non-candidate, cannot touch base,
cannot exceed the ceiling.

**Failure handling** (any of: aux disabled, no backend, timeout, HTTP 402/error,
malformed/non-JSON output): catch, set `aux_used=False`, record reason, proceed
with the deterministic result. Because the aux chain is currently dead, the
default `aux_enabled=False` means the selector is fully functional with the aux
stage compiled-in but never entered.

---

## 7. Allowlist / denylist / pins

- **Allowlist (ceiling):** the profile's enabled CLI toolsets. The final result
  is intersected with it (invariant 1). This is the security boundary — no rule,
  no keyword, no aux answer can grant a toolset the profile disabled.
- **Denylist:** `config.denylist` — toolsets never auto-selected even on a strong
  match (e.g. `computer_use`, `image_gen`, `spotify`, `homeassistant` on a code
  board). Removed from the candidate set before scoring. Cannot shadow base
  (load-time error, §4).
- **Pins (optional, forward-compat):** an operator may force-include toolsets via
  board/profile config (`toolset_selection.pin: [...]`). Pins are unioned into
  base *after* the ceiling intersection — a pin outside the ceiling is dropped
  and logged, never widening policy. Not required for v1; the schema reserves the
  field.

Order of operations: `ceiling` → drop `denylist` → `base ∪ pins` always in →
score `candidates` → threshold → aux(borderline) → **intersect with ceiling** →
dedup → sort.

---

## 8. Deduplication rules

- Operate at **toolset-name** granularity: casefold-normalize, drop names failing
  `validate_toolset()` (`toolsets.py:862-879`) with a logged warning, collapse to
  a `set`, emit sorted.
- **No tool-level dedup here.** Overlapping toolsets (e.g. `browser` bundles
  `web_search`, `toolsets.py:172-182`) are both kept as names; `resolve_toolset()`
  unions their tools into a set downstream (`toolsets.py:757-766`), so no tool is
  duplicated in the worker schema. Documented so implementers don't
  "optimize" by dropping a superset/subset name and accidentally remove a tool
  the other toolset didn't carry.

---

## 9. Fallback on error

Fail-open, mirroring today's `except → return None` in
`_resolve_worker_cli_toolsets` (`kanban_db.py:8542-8548`):

- Any exception inside `select_toolsets` → return
  `ToolsetSelection(toolsets=ceiling, fallback="selector_error:<ExcClass>")`.
- The caller already coalesces empty → `ceiling` → `None`
  (`return selection.toolsets or ceiling or None`).

Consequence: a selector bug degrades to **exactly today's behavior** (worker gets
the full profile toolset), never to a worker stranded with too few tools. Logged
at WARNING with the exception class and task id.

---

## 10. Observability

Emit **one structured record per selection** at dispatch time (JSON line in the
worker/dispatcher log, and optionally a `toolset_selection` telemetry event):

```json
{
  "event": "toolset_selection",
  "schema_version": 1,
  "selector_version": "1.0.0",
  "task_id": "t_8ab9bbbc", "run_id": 3, "board": "ra",
  "profile": "default", "executor": "hermes-worker",
  "mode": "narrow",
  "ceiling":   ["browser","file","image_gen","terminal","todo","vision","web", "..."],
  "base":      ["file","terminal","todo"],
  "selected":  ["file","terminal","todo","web"],
  "dropped":   ["browser","image_gen","vision", "..."],
  "matched": [
    {"toolset":"web","score":1.0,"confidence":0.5,
     "rule_ids":["kw.web"],"keywords":["docs"]}
  ],
  "borderline": [],
  "aux_enabled": false, "aux_used": false, "aux_latency_ms": null,
  "aux_delta": [],
  "confidence": 0.5,
  "fallback": null
}
```

Every narrowing is auditable: which toolsets, **why** (rule ids + matched
keywords per toolset), what was withheld (`dropped`), and whether aux moved
anything (`aux_delta`). This is also the tuning corpus — replay logged
title/body → expected set feeds the golden tests (`t_44c4a030`) and weight
tuning without touching logic.

Log levels: INFO for a normal selection, WARNING for `fallback` set or a dropped
invalid/out-of-ceiling name, DEBUG for full per-candidate score breakdown.

---

## 11. Config surface (`config.yaml`)

```yaml
kanban:
  toolset_selection:
    mode: off            # off | narrow. Default off — ship dark, enable per board.
    base: [file, terminal, todo]
    denylist: [computer_use, image_gen, spotify, homeassistant, tts]
    pin: []              # optional force-include (v1: reserved)
    tau_select: 0.5
    tau_low: 0.3
    aux:
      enabled: false     # aux chain currently dead; opt-in only
      timeout_s: 8
      model: auto        # follows auxiliary_client resolution chain
```

Resolved per-board/per-profile via the same `_get_platform_tools`/`load_config`
path already used in `_resolve_worker_cli_toolsets`.

---

## 12. Rollout

1. Ship module + call site with `mode: off` globally → zero behavior change;
   selector runs and **logs** what it *would* pick (shadow mode) if desired
   (compute selection, log it, but return `ceiling`). Recommended first step:
   collect a week of `toolset_selection` logs to validate the rule table.
2. Flip `mode: narrow` on one low-risk board; watch for "worker missing a tool"
   incidents (none expected — fail-open + generous base).
3. Roll out board-by-board. Enable `aux.enabled` only once the aux/OpenRouter
   chain is revived (currently dead).

---

## 13. Test plan pointer (implemented under `t_44c4a030`)

- **Golden table**: `(title, body, ceiling, config) → expected toolsets`. One
  case per rule (capability + keyword, EN and RU), incl. minimal-task → base only.
- **Invariants (property-style)**: result ⊆ ceiling; base∩ceiling ⊆ result;
  deterministic (no randomness); dedup/sort.
- **Denylist**: keyword match on a denylisted toolset → excluded.
- **Thresholds**: score at `tau_select`, just below, and in the borderline band.
- **Aux**: mocked `call_llm` — narrow-only enforced (aux returns out-of-ceiling →
  discarded); aux timeout/402/malformed → deterministic result, `aux_used=False`.
- **Fallback**: forced exception → returns ceiling with `fallback` set.
- **Executor bypass**: `claude-code`/`codex` → ceiling unchanged.
- **Integration**: `_resolve_worker_cli_toolsets(home, task, board)` with an
  isolated `HERMES_KANBAN_HOME`/`HERMES_HOME` temp profile; assert the `--toolsets`
  csv in the spawned argv. (Per project rule: isolated board in a temp dir, torn
  down after — never the live board.)

---

## 14. Files touched (for the implementation epic `t_5d8702b9`)

- **new** `hermes_cli/toolset_selector.py` — `SelectorConfig`, `ToolsetSelection`,
  `MatchReason`, the rule table (data), `select_toolsets`, `load_selector_config`,
  `emit_selection_log`.
- **edit** `hermes_cli/kanban_db.py` — `_resolve_worker_cli_toolsets` signature
  `(hermes_home, task, board)` + call into the selector; caller `_default_spawn`
  passes `task`, `board` (both already in scope).
- **edit** `config.yaml` example + `hermes_cli/config.py` schema/defaults for the
  `kanban.toolset_selection` block.
- **new** `tests/hermes_cli/test_toolset_selector.py` (owned by `t_44c4a030`).
- **new** doc `docs/design/toolset-selection.md` (this design, landed in-repo).

No change to `toolsets.py`, `model_tools.py`, or the ACP executor path.
