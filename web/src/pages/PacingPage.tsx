import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { api } from "@/lib/api";
import type { ZeusPacing, ZeusPacingPocket } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

// Poll cadence for the live "statusline" refresh. The external pacing
// controller rewrites pacing_state every few minutes, so 5s is plenty to feel
// live without hammering the read-only endpoint.
const REFRESH_MS = 5000;

type Tone = "default" | "destructive" | "outline" | "secondary" | "success" | "warning";

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

function formatPercent(p: number | null): string {
  return p == null ? "—" : `${Math.round(p)}%`;
}

// Coarse "time until reset" for the statusline (through Xд Yч / Xч Yм / Xм).
function formatCountdown(seconds: number | null): string {
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

// Fill colour for the spent bar: red near/over the cap, amber when spending
// ahead of pace, green when comfortably on track, muted at rest.
function spentBarClass(p: ZeusPacingPocket): string {
  if (p.cooling) return "bg-destructive";
  const spent = p.spent_percent ?? 0;
  if (spent >= 90) return "bg-destructive";
  if (p.on_track === false) return "bg-warning";
  if (p.on_track === true) return "bg-success";
  return "bg-muted-foreground";
}

// The statusline meter: a spent-fill bar with target + elapsed tick marks
// overlaid, so the operator sees "how much used vs how much I should have used
// by now vs how far into the window we are" at a glance.
function PaceMeter({ pocket }: { pocket: ZeusPacingPocket }) {
  const spent = Math.max(0, Math.min(100, pocket.spent_percent ?? 0));
  const target = pocket.target_percent;
  const elapsed = pocket.elapsed_percent;
  const clamp = (v: number) => `${Math.max(0, Math.min(100, v))}%`;
  return (
    <div className="relative h-3 w-full overflow-hidden rounded-sm bg-muted">
      <div
        className={cn("absolute inset-y-0 left-0 rounded-sm", spentBarClass(pocket))}
        style={{ width: `${spent}%` }}
      />
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

function PaceDelta({ pocket }: { pocket: ZeusPacingPocket }) {
  if (pocket.pace_delta == null) return null;
  const ahead = pocket.pace_delta > 0;
  const text = ahead
    ? `опережаем цель на ${Math.abs(pocket.pace_delta)}%`
    : `запас ${Math.abs(pocket.pace_delta)}% до цели`;
  return (
    <span className={cn(ahead ? "text-warning" : "text-success")}>{text}</span>
  );
}

function PocketCard({ pocket }: { pocket: ZeusPacingPocket }) {
  const wt = pocket.window_tokens;
  return (
    <Card className={cn(pocket.cooling && "border-destructive/60")}>
      <CardContent className="flex flex-col gap-2 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-medium">{pocket.display_name}</span>
          <Badge tone={modeTone(pocket.mode)} className="shrink-0">
            {MODE_LABEL[pocket.mode] ?? pocket.mode}
          </Badge>
          {pocket.agent_limit != null && (
            <Badge tone="outline" className="shrink-0">
              лимит агентов: {pocket.agent_limit}
            </Badge>
          )}
          {pocket.cooling && (
            <Badge tone="destructive" className="shrink-0">
              остывает
            </Badge>
          )}
          {pocket.enabled === false && (
            <Badge tone="secondary" className="shrink-0">
              выкл
            </Badge>
          )}
          <span className={cn("ml-auto text-sm", themedBody)}>
            до сброса: <span className="font-medium">{formatCountdown(pocket.seconds_to_reset)}</span>
          </span>
        </div>

        <PaceMeter pocket={pocket} />

        <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 text-sm", themedBody)}>
          <span>
            выбрано <span className="font-medium">{formatPercent(pocket.spent_percent)}</span>
          </span>
          <span className="text-muted-foreground">
            цель {formatPercent(pocket.target_percent)} · прошло {formatPercent(pocket.elapsed_percent)}
          </span>
          <span className="mx-1 text-muted-foreground">·</span>
          <PaceDelta pocket={pocket} />
        </div>

        <div className={cn("flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground", themedBody)}>
          <span>
            за окно: <span className="font-medium text-foreground">{formatTokens(wt?.total_tokens ?? null)}</span> токенов
            {wt ? ` (${wt.turns} ходов)` : ""}
          </span>
          {pocket.burn_rate_per_min != null && (
            <span>· {formatTokens(Math.round(pocket.burn_rate_per_min))}/мин</span>
          )}
          <span>· обновлено {formatAge(pocket.staleness_seconds)}</span>
        </div>

        {pocket.reason && (
          <div className={cn("truncate text-xs text-muted-foreground", themedBody)} title={pocket.reason}>
            {pocket.reason}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function PacingPage() {
  const [pacing, setPacing] = useState<ZeusPacing | null>(null);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  // Only surface the load error once per outage so a failing poll doesn't
  // spam a toast every REFRESH_MS.
  const erroredRef = useRef(false);

  const load = useCallback(() => {
    api
      .getZeusPacing()
      .then((data) => {
        setPacing(data);
        erroredRef.current = false;
      })
      .catch(() => {
        if (!erroredRef.current) {
          erroredRef.current = true;
          showToast("Не удалось загрузить пейсинг", "error");
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

  const pockets = pacing?.pockets ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <H2>Токены / Лимиты</H2>
        {pacing && (
          <Badge tone="outline" className="gap-1">
            за окно всего: {formatTokens(pacing.window_total_tokens)}
          </Badge>
        )}
      </div>
      <p className={cn("text-sm text-muted-foreground", themedBody)}>
        Расход подписок как в статуслайне терминала: по каждому «кармашку» — сколько выбрано, сколько
        до сброса окна и успеваем ли по темпу. Метка цели и прошедшего времени на полоске показывают,
        опережаем мы бюджет или есть запас. Обновляется каждые {REFRESH_MS / 1000}с.
      </p>

      {pockets.length === 0 && (
        <div className={cn("text-sm text-muted-foreground", themedBody)}>
          Данных пейсинга нет (плагин zeus не активен для этой доски).
        </div>
      )}

      <div className="flex flex-col gap-2">
        {pockets.map((p) => (
          <PocketCard key={p.subscription} pocket={p} />
        ))}
      </div>

      <Toast toast={toast} />
    </div>
  );
}
