import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { api } from "@/lib/api";
import type { ZeusPacingPocket, SubscriptionPoolEntry, Problem } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

// Poll cadence for the live "statusline" refresh. The external pacing
// controller rewrites pacing_state every few minutes and the pool/lease read is
// a cheap local sqlite scan, so 5s is plenty to feel live without hammering.
const REFRESH_MS = 5000;

type Tone = "default" | "destructive" | "outline" | "secondary" | "success" | "warning";

// One subscription pocket, merged from the pool registry (health, who's using,
// reserved) and the pacing snapshot (window curves, forecast, burn). ``pacing``
// is null for a pocket with no pacing_state row yet (disabled / never leased).
interface Sub {
  name: string;
  displayName: string;
  enabled: boolean;
  reserved: boolean;
  loggedIn: boolean;
  dirExists: boolean;
  cooling: boolean;
  coolingUntil: number | null;
  activeSessions: number;
  maxConcurrency: number;
  burnPerHour: number | null;
  pacing: ZeusPacingPocket | null;
}

type SubState = "active" | "idle" | "paused";

// Compact token count (410000 -> "410K", 1.5e6 -> "1.5M"). Mirrors
// zeus_tokens.humanize_tokens on the backend.
function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1e6) {
    const v = n / 1e6;
    const r = Math.round(v);
    return `${Math.abs(v - r) < 0.05 ? r : v.toFixed(1)}M`;
  }
  if (n >= 1e3) {
    const v = n / 1e3;
    const r = Math.round(v);
    return `${Math.abs(v - r) < 0.05 ? r : v.toFixed(1)}K`;
  }
  return String(n);
}

function formatPercent(p: number | null | undefined): string {
  return p == null ? "—" : `${Math.round(p)}%`;
}

// Coarse "time until reset" for the statusline (Xд Yч / Xч Yм / Xм).
function formatCountdown(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds <= 0) return "сброс";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}д ${h}ч`;
  if (h > 0) return `${h}ч ${m}м`;
  return `${m}м`;
}

function formatAge(seconds: number | null): string {
  if (seconds == null) return "n/a";
  if (seconds < 90) return `${Math.round(seconds)}с назад`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}м назад`;
  return `${Math.round(seconds / 3600)}ч назад`;
}

// Russian count agreement: 1 воркер, 2 воркера, 5 воркеров.
function pluralRu(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const MODE_LABEL: Record<string, string> = {
  throttle: "тормозим",
  burndown: "дожигаем",
  idle: "покой",
};

function modeTone(mode: string): Tone {
  if (mode === "throttle") return "warning";
  if (mode === "burndown") return "secondary";
  return "outline";
}

const STATE_META: Record<SubState, { label: string; hint: string }> = {
  active: { label: "Активные", hint: "прямо сейчас кто-то расходует" },
  idle: { label: "Простаивают", hint: "готовы к работе, но не используются" },
  paused: { label: "На паузе", hint: "остывают, выключены или без входа" },
};

function subState(s: Sub): SubState {
  if (!s.enabled || !s.loggedIn || s.cooling) return "paused";
  if (s.activeSessions > 0) return "active";
  return "idle";
}

// Provider-health verdict. A logged-out pocket is genuinely broken (auth death)
// and deep-links to the Проблемы board; cooling/disabled are expected pauses.
function health(s: Sub): { label: string; tone: Tone; broken: boolean } {
  if (!s.dirExists) return { label: "нет конфига", tone: "destructive", broken: true };
  if (!s.loggedIn) return { label: "нет входа", tone: "destructive", broken: true };
  if (!s.enabled) return { label: "выключена", tone: "secondary", broken: false };
  if (s.cooling) return { label: "остывает", tone: "warning", broken: false };
  return { label: "здорова", tone: "success", broken: false };
}

// "Кто её сейчас использует": worker leases + the reserve contract.
function usageLabel(s: Sub): string {
  const parts: string[] = [];
  if (s.activeSessions > 0) {
    const noun = pluralRu(s.activeSessions, "воркер", "воркера", "воркеров");
    parts.push(`${s.activeSessions} ${noun}`);
  }
  if (s.reserved) parts.push("резерв оператора");
  if (parts.length === 0) return "свободна";
  return parts.join(" · ");
}

function burnPerHour(s: Sub): number | null {
  if (s.burnPerHour != null) return s.burnPerHour;
  const perMin = s.pacing?.burn_rate_per_min;
  return perMin != null ? Math.round(perMin * 60) : null;
}

// End-of-week forecast at the current burn: will the weekly window make it to
// reset, or run into the wall first? Driven by the circuit-breaker projection.
function forecast(p: ZeusPacingPocket | null): { text: string; tone: Tone } | null {
  const proj = p?.circuit_breaker?.projected_spent_percent;
  if (proj == null) return null;
  if (proj >= 100) {
    return { text: `по темпу упрётся в лимит до сброса (~${Math.round(proj)}%)`, tone: "warning" };
  }
  return { text: `успевает до сброса (~${Math.round(proj)}% к концу недели)`, tone: "success" };
}

function mergeSubs(pool: SubscriptionPoolEntry[], pockets: ZeusPacingPocket[]): Sub[] {
  const byName = new Map<string, ZeusPacingPocket>();
  for (const p of pockets) byName.set(p.subscription, p);
  const subs: Sub[] = pool.map((e) => ({
    name: e.name,
    displayName: e.display_name || e.name,
    enabled: e.enabled,
    reserved: e.reserved,
    loggedIn: e.logged_in,
    dirExists: e.dir_exists,
    cooling: e.cooling,
    coolingUntil: e.cooling_until,
    activeSessions: e.active_sessions,
    maxConcurrency: e.max_concurrency,
    burnPerHour: e.burn_rate_tokens_per_hour,
    pacing: byName.get(e.name) ?? null,
  }));
  // A pocket the pool doesn't know about (pacing-only) still deserves a card.
  const known = new Set(subs.map((s) => s.name));
  for (const p of pockets) {
    if (known.has(p.subscription)) continue;
    subs.push({
      name: p.subscription,
      displayName: p.display_name || p.subscription,
      enabled: p.enabled ?? true,
      reserved: p.reserved,
      loggedIn: true,
      dirExists: true,
      cooling: p.cooling,
      coolingUntil: p.cooling_until,
      activeSessions: 0,
      maxConcurrency: 0,
      burnPerHour: null,
      pacing: p,
    });
  }
  return subs;
}

// A spent-fill bar with optional target + elapsed tick marks (weekly only —
// the 5h session has no controller target).
function Bar({
  spent,
  target,
  elapsed,
  tone,
}: {
  spent: number | null | undefined;
  target?: number | null;
  elapsed?: number | null;
  tone: string;
}) {
  const s = Math.max(0, Math.min(100, spent ?? 0));
  const clamp = (v: number) => `${Math.max(0, Math.min(100, v))}%`;
  return (
    <div className="relative h-2.5 w-full overflow-hidden rounded-sm bg-muted">
      <div className={cn("absolute inset-y-0 left-0 rounded-sm", tone)} style={{ width: `${s}%` }} />
      {target != null && (
        <div
          className="absolute inset-y-0 w-0.5 bg-foreground/70"
          style={{ left: clamp(target) }}
          title={`Цель ${formatPercent(target)}`}
        />
      )}
      {elapsed != null && (
        <div
          className="absolute inset-y-0 w-px bg-foreground/40"
          style={{ left: clamp(elapsed) }}
          title={`Прошло окна ${formatPercent(elapsed)}`}
        />
      )}
    </div>
  );
}

function barTone(spent: number | null | undefined, cooling: boolean): string {
  if (cooling) return "bg-destructive";
  const v = spent ?? 0;
  if (v >= 90) return "bg-destructive";
  if (v >= 70) return "bg-warning";
  return "bg-success";
}

// One labelled window row: title, bar, spent% + time-to-reset.
function WindowRow({
  title,
  spent,
  target,
  elapsed,
  secondsToReset,
  cooling,
  suffix,
}: {
  title: string;
  spent: number | null | undefined;
  target?: number | null;
  elapsed?: number | null;
  secondsToReset: number | null | undefined;
  cooling: boolean;
  suffix?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className={cn("flex items-baseline justify-between text-xs", themedBody)}>
        <span className="text-muted-foreground">{title}</span>
        <span>
          <span className="font-medium text-foreground">{formatPercent(spent)}</span>
          <span className="text-muted-foreground"> · сброс {formatCountdown(secondsToReset)}</span>
          {suffix && <span className="text-muted-foreground"> · {suffix}</span>}
        </span>
      </div>
      <Bar spent={spent} target={target} elapsed={elapsed} tone={barTone(spent, cooling)} />
    </div>
  );
}

function SubCard({ sub }: { sub: Sub }) {
  const p = sub.pacing;
  const h = health(sub);
  const fc = forecast(p);
  const bph = burnPerHour(sub);
  const fiveSpent = p?.circuit_breaker_5h?.live_spent_percent ?? null;
  const fiveReset = p?.five_hour_window?.seconds_to_reset ?? null;
  const fiveTokens = p?.five_hour_window?.tokens?.total_tokens ?? null;
  return (
    <Card className={cn(sub.cooling && "border-destructive/60")}>
      <CardContent className="flex flex-col gap-3 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-medium">{sub.displayName}</span>
          {h.broken ? (
            <Link to="/problems">
              <Badge tone={h.tone} className="shrink-0">
                {h.label} → Проблемы
              </Badge>
            </Link>
          ) : (
            <Badge tone={h.tone} className="shrink-0">
              {h.label}
            </Badge>
          )}
          {sub.reserved && (
            <Badge tone="outline" className="shrink-0">
              резерв
            </Badge>
          )}
          {p && (
            <Badge tone={modeTone(p.mode)} className="shrink-0">
              {MODE_LABEL[p.mode] ?? p.mode}
            </Badge>
          )}
          {p?.agent_limit != null && (
            <Badge tone="outline" className="shrink-0">
              лимит агентов: {p.agent_limit}
            </Badge>
          )}
          <span className={cn("ml-auto text-sm", themedBody)}>{usageLabel(sub)}</span>
        </div>

        <WindowRow
          title="5-часовое окно"
          spent={fiveSpent}
          secondsToReset={fiveReset}
          cooling={sub.cooling}
          suffix={fiveTokens != null ? `${formatTokens(fiveTokens)} ток` : undefined}
        />
        <WindowRow
          title="Недельный лимит"
          spent={p?.spent_percent}
          target={p?.target_percent}
          elapsed={p?.elapsed_percent}
          secondsToReset={p?.seconds_to_reset}
          cooling={sub.cooling}
        />

        <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 text-xs", themedBody)}>
          <span className="text-muted-foreground">
            расход: <span className="font-medium text-foreground">{formatTokens(bph)}</span>/ч
          </span>
          {fc && (
            <>
              <span className="text-muted-foreground">·</span>
              <span className={cn(fc.tone === "warning" ? "text-warning" : "text-success")}>
                {fc.text}
              </span>
            </>
          )}
          {p?.staleness_seconds != null && (
            <span className="ml-auto text-muted-foreground">обновлено {formatAge(p.staleness_seconds)}</span>
          )}
        </div>

        {p?.reason && (
          <div className={cn("truncate text-xs text-muted-foreground", themedBody)} title={p.reason}>
            {p.reason}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function StateGroup({ state, subs }: { state: SubState; subs: Sub[] }) {
  if (subs.length === 0) return null;
  const meta = STATE_META[state];
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{meta.label}</h3>
        <Badge tone="outline" className="shrink-0">
          {subs.length}
        </Badge>
        <span className={cn("text-xs text-muted-foreground", themedBody)}>{meta.hint}</span>
      </div>
      {subs.map((s) => (
        <SubCard key={s.name} sub={s} />
      ))}
    </div>
  );
}

export default function PacingPage() {
  const [subs, setSubs] = useState<Sub[] | null>(null);
  const [problemCount, setProblemCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  // Surface a load failure only once per outage so a failing poll doesn't spam
  // a toast every REFRESH_MS.
  const erroredRef = useRef(false);

  const load = useCallback(() => {
    Promise.all([api.getSubscriptionPool(), api.getZeusPacing(), api.getProblems()])
      .then(([pool, pacing, problems]) => {
        setSubs(mergeSubs(pool.subscriptions ?? [], pacing.pockets ?? []));
        setProblemCount(
          (problems.problems ?? []).filter((x: Problem) => x.source === "subscription-limits")
            .length,
        );
        erroredRef.current = false;
      })
      .catch(() => {
        if (!erroredRef.current) {
          erroredRef.current = true;
          showToast("Не удалось загрузить состояние подписок", "error");
        }
      })
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    load();
    const id = window.setInterval(() => {
      // Skip polling while the tab is hidden — resume on next visible tick.
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [load]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  const all = subs ?? [];
  const grouped: Record<SubState, Sub[]> = { active: [], idle: [], paused: [] };
  for (const s of all) grouped[subState(s)].push(s);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <H2>Подписки</H2>
        {problemCount > 0 && (
          <Link to="/problems">
            <Badge tone="warning" className="gap-1">
              провайдеры: {problemCount} {pluralRu(problemCount, "проблема", "проблемы", "проблем")}
            </Badge>
          </Link>
        )}
      </div>
      <p className={cn("text-sm text-muted-foreground", themedBody)}>
        Живое состояние всех карманов: по каждому — 5-часовое окно и недельный лимит (полоска +
        сколько выбрано + когда сброс), скорость расхода, прогноз «успеет ли до конца недели» по
        текущему темпу и кто её сейчас держит. Метка цели и прошедшего времени на недельной полоске
        показывают, опережаем мы бюджет или есть запас. Обновляется каждые {REFRESH_MS / 1000}с.
      </p>

      {all.length === 0 && (
        <div className={cn("text-sm text-muted-foreground", themedBody)}>
          Подписок нет (пул zeus не сконфигурирован для этой доски).
        </div>
      )}

      <StateGroup state="active" subs={grouped.active} />
      <StateGroup state="idle" subs={grouped.idle} />
      <StateGroup state="paused" subs={grouped.paused} />

      <Toast toast={toast} />
    </div>
  );
}
