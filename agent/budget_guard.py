"""Payment-source-aware daily spend guard for tool execution.

Meters live-API-key spend against a daily dollar limit and can HARD-STOP
non-exempt tools once the limit is reached. Subscription spend (billed by a
flat-rate plan, not per token) is shadow-tracked but NEVER blocks — it only
raises soft warnings, because its marginal dollar cost is really $0.

A short exempt-list of protocol tools (``kanban_block``/``kanban_comment``/
``propose_decision``) is always allowed even past a hard stop, so a throttled
worker can still report status, block, or escalate. The 2026-07-16 incident:
a worker hit the limit and could not even file a block, the dispatcher read
that as a protocol violation, and the operator never got a signal.

The daily hard stop is ON by default (fail-closed): an unattended nightly run
must not be able to burn live-API-key dollars without a ceiling. The default cap
is deliberately generous so ordinary work never trips it, and a single session
can consciously opt out for one run via ``HERMES_BUDGET_GUARD_OFF=1`` (logged, so
the opt-out is never silent). ``before_call`` returns a
:class:`agent.tool_guardrails.ToolGuardrailDecision` so the runtime wires it
through the exact same block path as the loop guardrail.

Broken metering must not silently read $0 — a dead pricing feed would leave the
hard stop toothless. :func:`feed_budget_guard` reports failures to the guard via
:meth:`BudgetGuard.record_metering_failure`, which logs them and, once they
persist, fails closed instead of pretending spend is zero.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Optional

from agent.tool_guardrails import ToolGuardrailDecision

_logger = logging.getLogger(__name__)

SpendSource = Literal["api_key", "subscription"]

# Per-session, conscious opt-out of the daily hard stop. Truthy disables the hard
# stop for this process only, without touching the shared config.yaml.
BUDGET_GUARD_OFF_ENV = "HERMES_BUDGET_GUARD_OFF"

# Protocol lifelines: a throttled worker must always be able to report a block,
# comment, or escalate a decision, so these bypass any hard stop.
EXEMPT_PROTOCOL_TOOLS = frozenset(
    {"kanban_block", "kanban_comment", "propose_decision"}
)

# Providers whose spend is covered by a flat-rate subscription/OAuth plan rather
# than metered per-token API-key billing. Spend on these never hard-stops.
# "local" is the on-device model server (Ollama/vLLM): flat-rate $0 spend.
# Classifying it as metered would flag every turn as an unpriced api-key
# call and fail-close the guard with budget_metering_dead.
# zai / kimi-coding are flat-rate coding plans in this deployment; their
# models are absent from the pricing feed, and metered classification would
# flag every turn as an unpriced api-key call and fail-close the worker
# (budget_metering_dead) - the qwen/glm night-cascade of 2026-07-19.
_SUBSCRIPTION_PROVIDERS = frozenset(
    {"openai-codex", "xai-oauth", "local", "zai", "kimi-coding"}
)


def classify_spend_source(
    provider: Optional[str], cost_status: Optional[str]
) -> SpendSource:
    """Classify a turn's spend as subscription (flat-rate) vs metered api_key.

    ``cost_status == "included"`` is the authoritative signal that pricing
    resolved the route to a subscription plan; a subscription/OAuth provider is
    the fallback for routes we could not price. Everything else is treated as
    live-API-key spend, which is the only kind that can hard-stop.
    """
    if cost_status == "included":
        return "subscription"
    if (provider or "").strip().lower() in _SUBSCRIPTION_PROVIDERS:
        return "subscription"
    return "api_key"


@dataclass(frozen=True)
class BudgetGuardConfig:
    """Daily spend thresholds and the protocol-tool exempt list.

    ``hard_stop_enabled`` defaults to ``True`` (fail-closed): the generous
    default cap bounds an unattended run out of the box. Operators can lower the
    cap or turn the stop off in the ``budget_guard`` config.yaml section, and a
    single session can opt out via the ``HERMES_BUDGET_GUARD_OFF`` env var (see
    :meth:`with_session_overrides`). ``metering_dead_after`` is how many
    consecutive metering failures mark the pricing feed dead (see
    :meth:`BudgetGuard.record_metering_failure`).
    """

    daily_limit_usd: float = 250.0
    warn_pct: float = 0.75
    hard_pct: float = 1.0
    hard_stop_enabled: bool = True
    warnings_enabled: bool = True
    metering_dead_after: int = 3
    exempt_tools: frozenset[str] = field(default_factory=lambda: EXEMPT_PROTOCOL_TOOLS)

    @property
    def warn_threshold_usd(self) -> float:
        return self.daily_limit_usd * self.warn_pct

    @property
    def hard_threshold_usd(self) -> float:
        return self.daily_limit_usd * self.hard_pct

    def with_session_overrides(
        self, env: Mapping[str, str] | None = None
    ) -> "BudgetGuardConfig":
        """Apply per-session env overrides — currently the conscious opt-out.

        ``HERMES_BUDGET_GUARD_OFF=1`` disables the daily hard stop for this
        session only, without editing the shared config.yaml. A deliberate,
        visible knob for an operator who needs one run past the cap; the caller
        logs when it takes effect so the opt-out is never silent.
        """
        source = os.environ if env is None else env
        if self.hard_stop_enabled and _as_bool(source.get(BUDGET_GUARD_OFF_ENV), False):
            return replace(self, hard_stop_enabled=False)
        return self

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "BudgetGuardConfig":
        """Build config from the ``budget_guard`` config.yaml section."""
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        exempt = data.get("exempt_tools")
        exempt_set = (
            frozenset(str(t) for t in exempt)
            if isinstance(exempt, (list, tuple, set, frozenset))
            else defaults.exempt_tools
        )
        return cls(
            daily_limit_usd=_as_float(data.get("daily_limit_usd"), defaults.daily_limit_usd),
            warn_pct=_as_float(data.get("warn_pct"), defaults.warn_pct),
            hard_pct=_as_float(data.get("hard_pct"), defaults.hard_pct),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            metering_dead_after=_as_int(
                data.get("metering_dead_after"), defaults.metering_dead_after
            ),
            exempt_tools=exempt_set,
        )


class BudgetGuard:
    """Stateful controller: accumulates spend by source, gates tools on it.

    The runtime feeds each turn's cost via :meth:`record_spend` (metered dollars
    for API-key turns, shadow dollars for subscription turns) and calls
    :meth:`before_call` per tool. The controller is otherwise side-effect free —
    surfacing warnings/blocks to the model or operator is the runtime's job.
    """

    def __init__(self, config: BudgetGuardConfig | None = None):
        self.config = config or BudgetGuardConfig()
        self._api_key_usd = 0.0
        self._subscription_usd = 0.0
        self._warned: set[str] = set()
        self._metering_failures = 0

    @property
    def api_key_spend_usd(self) -> float:
        return self._api_key_usd

    @property
    def subscription_spend_usd(self) -> float:
        return self._subscription_usd

    @property
    def metering_failures(self) -> int:
        return self._metering_failures

    @property
    def metering_broken(self) -> bool:
        """True once metering has failed enough times in a row to be untrusted."""
        return (
            self._metering_failures > 0
            and self._metering_failures >= self.config.metering_dead_after
        )

    def record_spend(self, amount_usd: float | None, source: SpendSource) -> None:
        """Accumulate a turn's spend into the bucket for its payment source."""
        try:
            amount = max(0.0, float(amount_usd or 0.0))
        except (TypeError, ValueError):
            return
        # A clean record proves the pricing feed is alive again.
        self._metering_failures = 0
        if source == "subscription":
            self._subscription_usd += amount
        else:
            self._api_key_usd += amount

    def record_metering_failure(self, error: object) -> None:
        """Count a metering/pricing failure and surface it — never a silent $0.

        A broken pricing feed would otherwise let spend read $0 forever, leaving
        the hard stop toothless. Consecutive failures are counted; once they
        cross ``metering_dead_after`` the guard treats metering as dead and (with
        the hard stop enabled) fails closed in :meth:`before_call`.
        """
        self._metering_failures += 1
        _logger.warning(
            "Budget guard metering failure #%d — spend can no longer be tracked; "
            "hard stop will fail closed once %d consecutive failures accrue: %s",
            self._metering_failures,
            self.config.metering_dead_after,
            error,
        )

    def before_call(
        self, tool_name: str, args: Mapping[str, Any] | None = None
    ) -> ToolGuardrailDecision:
        """Decide whether ``tool_name`` may run given accumulated spend."""
        cfg = self.config
        if tool_name in cfg.exempt_tools:
            return ToolGuardrailDecision(tool_name=tool_name)

        if self.metering_broken:
            dead = self._metering_dead_decision(tool_name)
            if dead is not None:
                return dead

        if (
            cfg.hard_stop_enabled
            and self._api_key_usd >= cfg.hard_threshold_usd
        ):
            return ToolGuardrailDecision(
                action="block",
                code="budget_hard_stop",
                message=(
                    f"Blocked {tool_name}: live-API-key spend "
                    f"${self._api_key_usd:.2f} reached the daily hard limit "
                    f"${cfg.hard_threshold_usd:.2f}. Protocol tools "
                    f"({', '.join(sorted(cfg.exempt_tools))}) stay available to "
                    "report a block or escalate; stop other work for today."
                ),
                tool_name=tool_name,
            )

        return self._warning_for(tool_name) or ToolGuardrailDecision(tool_name=tool_name)

    def _warning_for(self, tool_name: str) -> Optional[ToolGuardrailDecision]:
        """Emit a one-time soft warning per source once it crosses warn_pct."""
        cfg = self.config
        if not cfg.warnings_enabled:
            return None
        threshold = cfg.warn_threshold_usd
        if self._api_key_usd >= threshold and "api_key" not in self._warned:
            self._warned.add("api_key")
            return ToolGuardrailDecision(
                action="warn",
                code="budget_api_key_warning",
                message=(
                    f"Live-API-key spend ${self._api_key_usd:.2f} passed "
                    f"{cfg.warn_pct:.0%} of the ${cfg.daily_limit_usd:.2f} daily "
                    "limit; work will hard-stop at the limit."
                ),
                tool_name=tool_name,
            )
        if self._subscription_usd >= threshold and "subscription" not in self._warned:
            self._warned.add("subscription")
            return ToolGuardrailDecision(
                action="warn",
                code="budget_subscription_warning",
                message=(
                    f"Subscription (shadow) spend ${self._subscription_usd:.2f} "
                    f"passed {cfg.warn_pct:.0%} of the ${cfg.daily_limit_usd:.2f} "
                    "daily limit; subscription usage never blocks, this is FYI."
                ),
                tool_name=tool_name,
            )
        return None

    def _metering_dead_decision(
        self, tool_name: str
    ) -> Optional[ToolGuardrailDecision]:
        """Gate a tool when the pricing feed is dead (spend is no longer known)."""
        cfg = self.config
        if cfg.hard_stop_enabled:
            # Fail closed: with no trustworthy spend figure we cannot prove we
            # are under the cap, so stop non-exempt work rather than run blind.
            return ToolGuardrailDecision(
                action="block",
                code="budget_metering_dead",
                message=(
                    f"Blocked {tool_name}: budget metering has failed "
                    f"{self._metering_failures} times in a row, so live-API-key "
                    "spend can no longer be tracked. Failing closed to avoid "
                    "un-capped burn. Protocol tools "
                    f"({', '.join(sorted(cfg.exempt_tools))}) stay available to "
                    "report a block or escalate; fix pricing/metering or set "
                    f"{BUDGET_GUARD_OFF_ENV}=1 to override for this session."
                ),
                tool_name=tool_name,
            )
        if "metering_dead" not in self._warned:
            self._warned.add("metering_dead")
            return ToolGuardrailDecision(
                action="warn",
                code="budget_metering_dead_warning",
                message=(
                    f"Budget metering has failed {self._metering_failures} times "
                    "in a row; the daily cap cannot be enforced. FYI only — the "
                    "hard stop is disabled."
                ),
                tool_name=tool_name,
            )
        return None


def feed_budget_guard(
    guard: Optional[BudgetGuard],
    *,
    model: str,
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    usage: Any,
    cost_result: Any,
) -> None:
    """Record one turn's spend into ``guard``, tagged by payment source.

    Subscription turns are priced at their shadow (would-be-metered) rate so
    warnings are meaningful even though the billed amount is $0; api-key turns
    use the real estimated cost. Never raises — a broken feed must not break the
    agent loop — but it also never silently reads $0: a failure (an exception, or
    an api-key turn the pricing feed could not price) is reported to the guard so
    a systematically dead feed becomes visible and the hard stop fails closed.
    """
    if guard is None:
        return
    try:
        source = classify_spend_source(provider, getattr(cost_result, "status", None))
        if source == "subscription":
            from agent.usage_pricing import estimate_metered_cost

            shadow = estimate_metered_cost(
                model, usage, provider=provider, base_url=base_url, api_key=api_key
            )
            amount = shadow.amount_usd if shadow.amount_usd is not None else 0.0
            guard.record_spend(float(amount), "subscription")
        else:
            amount = getattr(cost_result, "amount_usd", None)
            if amount is None:
                # Unpriced api-key turn: recording $0 here is exactly the silent
                # under-count that leaves the hard stop toothless. Flag it.
                guard.record_metering_failure(
                    "api-key turn unpriced (cost amount_usd is None)"
                )
                return
            guard.record_spend(float(amount), "api_key")
    except Exception as exc:  # telemetry must not break the loop — but must not hide
        guard.record_metering_failure(exc)


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


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
