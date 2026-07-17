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

Design mirrors :mod:`agent.tool_guardrails`: warnings on by default, hard stops
are explicit opt-in (``hard_stop_enabled=False``), so a misconfigured limit can
never silently wedge a session — the hard threshold is unreachable until an
operator turns it on. ``before_call`` returns a
:class:`agent.tool_guardrails.ToolGuardrailDecision` so the runtime wires it
through the exact same block path as the loop guardrail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from agent.tool_guardrails import ToolGuardrailDecision

SpendSource = Literal["api_key", "subscription"]

# Protocol lifelines: a throttled worker must always be able to report a block,
# comment, or escalate a decision, so these bypass any hard stop.
EXEMPT_PROTOCOL_TOOLS = frozenset(
    {"kanban_block", "kanban_comment", "propose_decision"}
)

# Providers whose spend is covered by a flat-rate subscription/OAuth plan rather
# than metered per-token API-key billing. Spend on these never hard-stops.
_SUBSCRIPTION_PROVIDERS = frozenset({"openai-codex", "xai-oauth"})


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

    ``hard_stop_enabled`` defaults to ``False`` so the hard threshold is
    unreachable out of the box (warn-only everywhere); an operator opts into
    circuit-breaker behavior via the ``budget_guard`` config.yaml section.
    """

    daily_limit_usd: float = 100.0
    warn_pct: float = 0.75
    hard_pct: float = 1.0
    hard_stop_enabled: bool = False
    warnings_enabled: bool = True
    exempt_tools: frozenset[str] = field(default_factory=lambda: EXEMPT_PROTOCOL_TOOLS)

    @property
    def warn_threshold_usd(self) -> float:
        return self.daily_limit_usd * self.warn_pct

    @property
    def hard_threshold_usd(self) -> float:
        return self.daily_limit_usd * self.hard_pct

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

    @property
    def api_key_spend_usd(self) -> float:
        return self._api_key_usd

    @property
    def subscription_spend_usd(self) -> float:
        return self._subscription_usd

    def record_spend(self, amount_usd: float | None, source: SpendSource) -> None:
        """Accumulate a turn's spend into the bucket for its payment source."""
        try:
            amount = max(0.0, float(amount_usd or 0.0))
        except (TypeError, ValueError):
            return
        if source == "subscription":
            self._subscription_usd += amount
        else:
            self._api_key_usd += amount

    def before_call(
        self, tool_name: str, args: Mapping[str, Any] | None = None
    ) -> ToolGuardrailDecision:
        """Decide whether ``tool_name`` may run given accumulated spend."""
        cfg = self.config
        if tool_name in cfg.exempt_tools:
            return ToolGuardrailDecision(tool_name=tool_name)

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
    agent loop.
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
            guard.record_spend(float(amount) if amount is not None else 0.0, "api_key")
    except Exception:  # pragma: no cover - defensive: telemetry must not break the loop
        pass


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
