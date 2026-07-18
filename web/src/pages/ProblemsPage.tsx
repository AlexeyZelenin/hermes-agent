import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { api } from "@/lib/api";
import type { Problem, ProblemTone } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

// Severity → the board's three-rung diagnostic palette (amber/orange/red),
// kept identical to the kanban plugin so a problem reads the same colour
// wherever it surfaces. Worst-first so criticals never scroll off the top.
const TONE_ORDER: ProblemTone[] = ["critical", "error", "warning", "info"];
const TONE_COLOR: Record<ProblemTone, string> = {
  info: "#8a94a6",
  warning: "#ff9e3b",
  error: "#ff6b3d",
  critical: "#ff4d4d",
};
const TONE_LABEL: Record<ProblemTone, string> = {
  info: "инфо",
  warning: "внимание",
  error: "ошибка",
  critical: "критично",
};

// Human labels for the scanners that write findings (brand-as-config).
const SOURCE_LABEL: Record<string, string> = {
  "regular-crons": "регулярные",
  integrity: "целостность",
  reflection: "рефлексия",
  advisor: "советник",
  security: "безопасность",
  "security-review": "security-ревью",
  "cross-card": "кросс-карты",
};

function sourceLabel(source: string): string {
  return SOURCE_LABEL[source] ?? source;
}

function ProblemCard({
  problem,
  busy,
  onAccept,
  onDismiss,
}: {
  problem: Problem;
  busy: boolean;
  onAccept: (p: Problem) => void;
  onDismiss: (p: Problem) => void;
}) {
  const color = TONE_COLOR[problem.tone];
  return (
    <Card style={{ borderLeft: `3px solid ${color}` }}>
      <CardContent className="flex flex-col gap-3 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{problem.title}</span>
              <Badge
                tone="outline"
                className="shrink-0 gap-1"
                style={{ color, borderColor: color }}
              >
                <AlertTriangle className="h-3 w-3" />
                {TONE_LABEL[problem.tone]}
              </Badge>
              <Badge tone="secondary" className="shrink-0">
                {sourceLabel(problem.source)}
              </Badge>
              {problem.category && (
                <Badge tone="outline" className="shrink-0">
                  {problem.category}
                </Badge>
              )}
            </div>
            {problem.explanation && (
              <p
                className={cn(
                  "mt-2 whitespace-pre-wrap text-sm text-muted-foreground",
                  themedBody,
                )}
              >
                {problem.explanation}
              </p>
            )}
            {problem.proposed && (
              <div className="mt-2 rounded-md border border-border/60 bg-muted/30 p-2">
                <div
                  className={cn(
                    "text-[11px] font-semibold uppercase tracking-wide text-muted-foreground",
                    themedBody,
                  )}
                >
                  Предложенное решение
                </div>
                <p className="mt-1 whitespace-pre-wrap text-sm">{problem.proposed}</p>
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            ghost
            disabled={busy}
            onClick={() => onDismiss(problem)}
            className="h-7 gap-1 text-xs"
          >
            <X className="h-3.5 w-3.5" />
            Отклонить
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => onAccept(problem)}
            className="h-7 gap-1 text-xs"
          >
            <Check className="h-3.5 w-3.5" />
            В бэклог
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function ProblemsPage() {
  const [problems, setProblems] = useState<Problem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const { toast, showToast } = useToast();

  const load = useCallback(() => {
    api
      .getProblems("")
      .then((r) => setProblems(r.problems))
      .catch(() => showToast("Не удалось загрузить проблемы", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  const accept = useCallback(
    async (p: Problem) => {
      setBusyId(p.id);
      try {
        const r = await api.acceptProblem(p.id);
        if (r.ok) {
          showToast("Проблема принята в бэклог", "success");
          load();
        } else {
          showToast(r.error ?? "Не удалось принять проблему", "error");
        }
      } catch {
        showToast("Не удалось принять проблему", "error");
      } finally {
        setBusyId(null);
      }
    },
    [load, showToast],
  );

  const dismiss = useCallback(
    async (p: Problem) => {
      setBusyId(p.id);
      try {
        const r = await api.dismissProblem(p.id);
        if (r.ok) {
          showToast("Проблема отклонена", "success");
          load();
        } else {
          showToast(r.error ?? "Не удалось отклонить проблему", "error");
        }
      } catch {
        showToast("Не удалось отклонить проблему", "error");
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

  const groups = TONE_ORDER.map((tone) => ({
    tone,
    items: problems.filter((p) => p.tone === tone),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-6">
      <H2>Проблемы</H2>
      <p className={cn("text-sm text-muted-foreground", themedBody)}>
        Системные находки от рефлексии, советника, security-ревью и проверок целостности. Каждая — черновик:
        «В бэклог» превращает её в реальную задачу, «Отклонить» убирает с глаз.
      </p>

      {groups.length === 0 && (
        <div className={cn("text-sm text-muted-foreground", themedBody)}>
          Проблем нет — всё чисто.
        </div>
      )}

      {groups.map((group) => (
        <section key={group.tone} className="flex flex-col gap-2">
          <h3
            className={cn(
              "text-sm font-semibold uppercase tracking-wide text-muted-foreground",
              themedBody,
            )}
          >
            {TONE_LABEL[group.tone]} · {group.items.length}
          </h3>
          {group.items.map((p) => (
            <ProblemCard
              key={p.id}
              problem={p}
              busy={busyId === p.id}
              onAccept={accept}
              onDismiss={dismiss}
            />
          ))}
        </section>
      ))}

      <Toast toast={toast} />
    </div>
  );
}
