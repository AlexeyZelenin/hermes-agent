"""The "Engine Room" (машинное отделение) — the app's *under-the-hood* side.

Operator idea (t_dece1fa5): the app has two sides. (1) The **user** side — working
with tasks. (2) The **engine room** — how the machine is built inside: the system
prompts the autonomous roles run under, the native diagnostics, the registry of
Регулярные (crons), the decision ledger, the preference memory. The organizing
principle is that *all meta lives on the engine-room side*, so it never clutters
the user side — and that this is a **trust/transparency** product feature (an
autonomous agent is trusted more when its wiring is visible), not merely operator
diagnostics.

This module is the engine room's *home*: a single, grounded model the native
dashboard renders. It has three pillars:

* **roles** — the actual ``_SYSTEM_PROMPT`` each meta-role runs under, resolved
  *live* from its source module (grounded, never hand-copied, so it can't drift).
  A renamed/removed constant degrades that row to ``available=False`` rather than
  crashing the surface.
* **surfaces** — the catalogue that ties the subordinate engine-room panels
  together (token spend, ROI, Регулярные, decision inbox, diagnostics), each with
  its API route and the task that owns it.
* **substrate** — the *native* telemetry sources (real ``zeus.db`` / ``kanban.db``
  tables + the OTel export), documenting the decision to draw natively rather than
  embed Grafana. Presence is probed against the live stores, so the surface tells
  the truth about what data actually exists.

Everything is pure and dependency-injectable: :func:`resolve_roles`,
:func:`surfaces`, and :func:`substrate` take plain inputs and unit-test without a
live board, while :func:`engine_room_model` wires the real stores. Labels are RU
and live in one place ("brand-as-config"), matching :mod:`hermes_cli.regular_crons`.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Iterable, Optional


# --- Pillar 1: role system prompts ------------------------------------------

# Each descriptor points at the *live* constant its role runs under. ``module`` +
# ``attr`` are resolved lazily so a sibling refactor that renames a constant
# degrades one row (``available=False``) instead of breaking import. ``when``
# says what triggers the role; ``model_role`` maps to the board's model roster
# (see ``MODEL_MAP_ROLES`` in :mod:`hermes_cli.kanban_db`).
_ROLE_SPECS: tuple[dict[str, str], ...] = (
    {
        "key": "decomposer",
        "title": "Декомпозер",
        "module": "hermes_cli.kanban_decompose",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "Разбивает эпик на атомарные задачи доски.",
        "when": "по запросу (decompose)",
        "model_role": "strong",
    },
    {
        "key": "specifier",
        "title": "Спецификатор",
        "module": "hermes_cli.kanban_specify",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "Триаж входящей задачи: уточняет формулировку и критерии готовности.",
        "when": "по запросу (specify)",
        "model_role": "mid",
    },
    {
        "key": "batch_planner",
        "title": "Планировщик пакета",
        "module": "hermes_cli.kanban_batch_take",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "Упорядочивает выбранный пакет задач в план исполнения.",
        "when": "по запросу (batch take)",
        "model_role": "mid",
    },
    {
        "key": "profile_describer",
        "title": "Описатель профилей",
        "module": "hermes_cli.profile_describer",
        "attr": "_SYSTEM_PROMPT",
        "purpose": "Формулирует человекочитаемое описание профиля-исполнителя.",
        "when": "по запросу (describe profile)",
        "model_role": "cheap",
    },
    {
        "key": "goal_judge",
        "title": "Судья цели",
        "module": "hermes_cli.goals",
        "attr": "JUDGE_SYSTEM_PROMPT",
        "purpose": "Строго оценивает, достигнута ли цель автономным агентом.",
        "when": "по запросу (goal check)",
        "model_role": "strong",
    },
    {
        "key": "contract_drafter",
        "title": "Составитель контракта",
        "module": "hermes_cli.goals",
        "attr": "DRAFT_CONTRACT_SYSTEM_PROMPT",
        "purpose": "Превращает цель на естественном языке в структурный контракт готовности.",
        "when": "по запросу (draft contract)",
        "model_role": "mid",
    },
)


def resolve_role(spec: dict[str, str]) -> dict[str, Any]:
    """Resolve one role descriptor, reading its system prompt *live* from source.

    A missing module or renamed attribute degrades to ``available=False`` with a
    ``null`` prompt — the surface must never crash because a sibling refactor
    moved a constant. ``prompt_chars`` is the length so the UI can show weight
    without shipping the whole prompt in list views.
    """
    row: dict[str, Any] = {
        "key": spec["key"],
        "title": spec["title"],
        "purpose": spec["purpose"],
        "when": spec["when"],
        "model_role": spec["model_role"],
        "module": spec["module"],
        "attr": spec["attr"],
        "available": False,
        "system_prompt": None,
        "prompt_chars": 0,
    }
    try:
        mod = importlib.import_module(spec["module"])
        value = getattr(mod, spec["attr"])
    except (ImportError, AttributeError):
        return row
    if not isinstance(value, str) or not value.strip():
        return row
    row["available"] = True
    row["system_prompt"] = value
    row["prompt_chars"] = len(value)
    return row


def resolve_roles(specs: Iterable[dict[str, str]] = _ROLE_SPECS) -> list[dict[str, Any]]:
    """Resolve every role descriptor. Order follows ``_ROLE_SPECS`` (curation order)."""
    return [resolve_role(spec) for spec in specs]


# --- Pillar 2: subordinate surfaces -----------------------------------------

# The engine-room index. Each panel is its own task (the "подчинённые поверхности"
# in t_dece1fa5); this catalogue is the home that links them so the meta side has
# one door. ``route`` is a real, auth-gated API path; ``owner_task`` is the card
# that builds the panel.
_SURFACES: tuple[dict[str, str], ...] = (
    {
        "key": "regulars",
        "title": "Регулярные",
        "route": "/api/cron/registry",
        "owner_task": "t_cdbf72d2",
        "purpose": "Реестр кронов: ритм, назначение, состояние, расход токенов, аномалии.",
    },
    {
        "key": "token_panel",
        "title": "Токены / Лимиты",
        "route": "/api/zeus/pacing",
        "owner_task": "t_7d6b2cdc",
        "purpose": "Пейсинг подписок как статуслайн: по кармашкам — выбрано/цель/до сброса + расход за окно.",
    },
    {
        "key": "roi",
        "title": "ROI постройки",
        "route": "/api/kanban/roi",
        "owner_task": "t_f5d68657",
        "purpose": "Стоимость постройки фичи в токенах на карточке и в разрезе эпика.",
    },
    {
        "key": "decisions",
        "title": "Инбокс решений",
        "route": "/api/kanban/decisions",
        "owner_task": "t_6dc73752",
        "purpose": "Леджер решений автономного агента: что решено, почему, с чем свериться.",
    },
    {
        "key": "diagnostics",
        "title": "Диагностика",
        "route": "/api/kanban/diagnostics",
        "owner_task": "t_dece1fa5",
        "purpose": "Структурные сигналы бедствия по задачам (фантомные id, краш-луп, застревание).",
    },
    {
        "key": "logs",
        "title": "Логи",
        "route": "/api/kanban/engine-log",
        "owner_task": "t_adf37522",
        "purpose": "Единый лог: операторские/кроновые breadcrumbs + КЛИЕНТСКИЙ лог фронтенда "
                   "(UI-действия, WS-события, оптимистичные рендеры, ошибки) — ловит фронтенд-фантомы.",
    },
)


def surfaces(specs: Iterable[dict[str, str]] = _SURFACES) -> list[dict[str, Any]]:
    """The engine-room surface index (declarative catalogue, copied so callers can't mutate it)."""
    return [dict(s) for s in specs]


# --- Pillar 3: native telemetry substrate -----------------------------------

DB_ZEUS = "zeus.db"
DB_KANBAN = "kanban.db"
DB_OTEL = "otel"

# The native draw sources. Decision (t_dece1fa5): render НАТИВНО from these stores
# rather than embedding Grafana (no extra exposed port, no wrapper). Grafana stays
# an optional power-user export *through* the OTel endpoint (t_795601eb). ``table``
# is the real table name so presence can be probed; OTel has no local table.
_SUBSTRATE_SPECS: tuple[dict[str, Optional[str]], ...] = (
    {
        "key": "token_usage",
        "title": "Расход токенов",
        "db": DB_ZEUS,
        "table": "token_usage",
        "purpose": "Токены и стоимость за каждый ход API, помеченные task_id — питает токены/ROI.",
    },
    {
        "key": "pacing_state",
        "title": "Пейсинг / лимиты",
        "db": DB_ZEUS,
        "table": "pacing_state",
        "purpose": "Состояние ритма расхода и приближения к лимитам — питает надзор за ресурсами.",
    },
    {
        "key": "decisions",
        "title": "Леджер решений",
        "db": DB_ZEUS,
        "table": "decisions",
        "purpose": "Записанные решения агента — питает инбокс решений.",
    },
    {
        "key": "findings",
        "title": "Находки",
        "db": DB_ZEUS,
        "table": "findings",
        "purpose": "Аномалии, вытолкнутые кронами/сканами — питает Регулярные и диагностику.",
    },
    {
        "key": "task_events",
        "title": "Аудит задач",
        "db": DB_KANBAN,
        "table": "task_events",
        "purpose": "Аудит-трейл событий доски — питает таймлайн и диагностику задач.",
    },
    {
        "key": "engine_log",
        "title": "Единый лог",
        "db": DB_KANBAN,
        "table": "engine_log",
        "purpose": "Структурный лог движка: операторские/кроновые записи + клиентский лог "
                   "фронтенда — питает просмотр 'под капотом' и лог-вотчер.",
    },
    {
        "key": "otel",
        "title": "OTel-субстрат",
        "db": DB_OTEL,
        "table": None,
        "purpose": "Экспорт телеметрии наружу (t_795601eb) — сюда позже цепляется Grafana как опция.",
    },
)


def substrate(
    specs: Iterable[dict[str, Optional[str]]] = _SUBSTRATE_SPECS,
    *,
    zeus_tables: Optional[set[str]] = None,
    kanban_tables: Optional[set[str]] = None,
    otel_configured: bool = False,
) -> list[dict[str, Any]]:
    """The native telemetry sources, each with a *probed* ``present`` flag.

    ``present`` is grounded in the live stores: a zeus/kanban source is present iff
    its table exists in the corresponding ``*_tables`` set (``None`` set → store
    absent → ``present=False``); the OTel source reflects ``otel_configured``.
    Pure: callers inject the table sets so this unit-tests without opening a DB.
    """
    zeus_tables = zeus_tables or set()
    kanban_tables = kanban_tables or set()
    rows: list[dict[str, Any]] = []
    for spec in specs:
        db = spec["db"]
        table = spec["table"]
        if db == DB_ZEUS:
            present = table in zeus_tables
        elif db == DB_KANBAN:
            present = table in kanban_tables
        elif db == DB_OTEL:
            present = bool(otel_configured)
        else:
            present = False
        rows.append({
            "key": spec["key"],
            "title": spec["title"],
            "db": db,
            "table": table,
            "purpose": spec["purpose"],
            "present": present,
        })
    return rows


# --- Live probes (impure edges) ---------------------------------------------


def _table_names(conn: Any) -> set[str]:
    """Table names in an open sqlite connection; ``{}`` for ``None`` or on error."""
    if conn is None:
        return set()
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    except Exception:
        return set()


def _otel_configured() -> bool:
    """Best-effort: is an OTel exporter wired via the standard env vars?"""
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    )


# --- Assembly ---------------------------------------------------------------


def engine_room_model(
    *,
    zeus_conn: Any = None,
    kanban_conn: Any = None,
    otel_configured: Optional[bool] = None,
) -> dict[str, Any]:
    """Assemble the full engine-room model the dashboard renders.

    Roles read their prompts live from source; surfaces are the static index;
    substrate presence is probed against the passed connections (both optional —
    a missing store just yields ``present=False`` rows). ``otel_configured``
    defaults to inspecting the standard OTel env vars.
    """
    if otel_configured is None:
        otel_configured = _otel_configured()
    return {
        "roles": resolve_roles(),
        "surfaces": surfaces(),
        "substrate": substrate(
            zeus_tables=_table_names(zeus_conn),
            kanban_tables=_table_names(kanban_conn),
            otel_configured=otel_configured,
        ),
    }
