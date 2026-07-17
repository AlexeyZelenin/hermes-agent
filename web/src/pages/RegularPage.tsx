import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Pause, Play, Zap } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { api } from "@/lib/api";
import type { CronRegistry, CronRegistryRow } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

// The two-axis taxonomy the registry sorts by. Purpose is the primary grouping
// (labels come from the backend, which owns the brand-as-config strings); each
// group is then read cadence-first via the badge. Kept in one place so the
// ordering is obvious and matches the CLI (`hermes cron registry`).
const PURPOSE_ORDER = ["supervision", "reflection", "security", "resources", "other"];

function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function formatAge(seconds: number | null): string {
  if (seconds == null) return "n/a";
  if (seconds < 90) return `${Math.round(seconds)}с назад`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}м назад`;
  return `${Math.round(seconds / 3600)}ч назад`;
}

function anomalyLabel(kind: string, factor?: number, streak?: number): string {
  if (kind === "token_spike") return `всплеск токенов ×${factor ?? "?"}`;
  if (kind === "consecutive_failures") return `падает подряд ×${streak ?? "?"}`;
  return kind;
}

// The cron ticker is the heartbeat of every regular process; a stale heartbeat
// means the whole subsystem is down, so it gets a banner rather than a per-row
// signal. Threshold mirrors the CLI's `cron status` (3 intervals + slack).
function TickerHealth({ registry }: { registry: CronRegistry }) {
  const { heartbeat_age, success_age, interval_seconds } = registry.ticker;
  const stale = heartbeat_age == null || heartbeat_age > interval_seconds * 3 + 20;
  const failing = !stale && (success_age == null || success_age > interval_seconds * 3 + 20);
  const tone = stale ? "destructive" : failing ? "secondary" : "outline";
  const text = stale
    ? `Планировщик не отвечает (пульс ${formatAge(heartbeat_age)})`
    : failing
      ? `Пульс есть, но тики падают (успех ${formatAge(success_age)})`
      : `Планировщик активен (пульс ${formatAge(heartbeat_age)})`;
  return (
    <Badge variant={tone} className="gap-1">
      {(stale || failing) && <AlertTriangle className="h-3 w-3" />}
      {text}
    </Badge>
  );
}

function RegularRow({
  row,
  busy,
  onToggle,
}: {
  row: CronRegistryRow;
  busy: boolean;
  onToggle: (row: CronRegistryRow) => void;
}) {
  const outcome = row.last_status ?? "never";
  const errored = outcome === "error";
  return (
    <Card className={cn(row.anomalies.length > 0 && "border-destructive/60")}>
      <CardContent className="flex items-center justify-between gap-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{row.name}</span>
            <Badge variant="outline" className="shrink-0">
              {row.cadence_badge}
            </Badge>
            {!row.enabled && (
              <Badge variant="secondary" className="shrink-0">
                выкл
              </Badge>
            )}
            {row.anomalies.map((a) => (
              <Badge key={a.kind} variant="destructive" className="shrink-0 gap-1">
                {a.kind === "token_spike" ? (
                  <Zap className="h-3 w-3" />
                ) : (
                  <AlertTriangle className="h-3 w-3" />
                )}
                {anomalyLabel(a.kind, a.factor, a.streak)}
              </Badge>
            ))}
          </div>
          <div className={cn("mt-1 text-sm", themedBody("muted"))}>
            <span className={cn(errored && "text-destructive")}>
              {formatTime(row.last_run_at)} · исход: {outcome}
            </span>
            <span className="mx-2">·</span>
            <span>
              токены:{" "}
              {row.tokens
                ? `${row.tokens.display} (${row.tokens.run_count} зап., ${row.tokens.period_days}д)`
                : row.no_agent
                  ? "— (скрипт)"
                  : "—"}
            </span>
          </div>
          {errored && row.last_error && (
            <div className="mt-1 truncate text-xs text-destructive/80">{row.last_error}</div>
          )}
        </div>
        <Button
          variant={row.enabled ? "outline" : "default"}
          size="sm"
          disabled={busy}
          onClick={() => onToggle(row)}
          className="shrink-0 gap-1"
        >
          {row.enabled ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {row.enabled ? "Выкл" : "Вкл"}
        </Button>
      </CardContent>
    </Card>
  );
}

export default function RegularPage() {
  const [registry, setRegistry] = useState<CronRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const load = useCallback(() => {
    api
      .getCronRegistry("all", 30)
      .then(setRegistry)
      .catch(() => showToast("Не удалось загрузить реестр", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback(
    async (row: CronRegistryRow) => {
      setBusyId(row.id);
      const profile = row.profile ?? "default";
      try {
        if (row.enabled) {
          await api.pauseCronJob(row.id, profile);
        } else {
          await api.resumeCronJob(row.id, profile);
        }
        load();
      } catch {
        showToast("Не удалось переключить процесс", "error");
      } finally {
        setBusyId(null);
      }
    },
    [load, showToast],
  );

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  const rows = registry?.crons ?? [];
  const groups = PURPOSE_ORDER.map((purpose) => ({
    purpose,
    rows: rows.filter((r) => r.purpose === purpose),
  })).filter((g) => g.rows.length > 0);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <H2>Регулярные</H2>
        {registry && <TickerHealth registry={registry} />}
      </div>
      <p className={cn("text-sm", themedBody("muted"))}>
        Служебные процессы системы с ритмом — здоровье и расход токенов за 30 дней. Запланированные
        задачи с датой живут на доске, здесь только стоячие процессы.
      </p>

      {groups.length === 0 && (
        <div className={cn("text-sm", themedBody("muted"))}>Регулярных процессов нет.</div>
      )}

      {groups.map((group) => (
        <section key={group.purpose} className="flex flex-col gap-2">
          <h3 className={cn("text-sm font-semibold uppercase tracking-wide", themedBody("muted"))}>
            {group.rows[0].purpose_label}
          </h3>
          {group.rows.map((row) => (
            <RegularRow
              key={row.id}
              row={row}
              busy={busyId === row.id}
              onToggle={toggle}
            />
          ))}
        </section>
      ))}

      {toast && <Toast message={toast.message} type={toast.type} />}
    </div>
  );
}
