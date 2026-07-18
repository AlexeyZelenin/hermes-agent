import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Cog, RotateCw } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { api } from "@/lib/api";
import type {
  EngineRoomModel,
  EngineRoomRole,
  EngineRoomSubstrate,
  EngineRoomSurface,
} from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";

// The engine room is a *transparent window into the same live state* the user
// side works over — not separate logic. This page renders the grounded model
// from hermes_cli/engine_room.py: the role prompts each meta-role runs under,
// the catalogue of under-the-hood surfaces, and the telemetry substrate.

function SectionHeading({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className="font-display text-sm uppercase tracking-[0.12em] text-text-tertiary">
        {title}
      </h3>
      <p className={cn("text-sm text-muted-foreground", themedBody)}>{hint}</p>
    </div>
  );
}

function RoleCard({ role }: { role: EngineRoomRole }) {
  const [open, setOpen] = useState(false);
  const hasPrompt = role.available && role.system_prompt;
  return (
    <Card className={cn(!role.available && "opacity-60")}>
      <CardContent className="flex flex-col gap-2 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{role.title}</span>
          <Badge tone="outline" className="shrink-0">
            {role.model_role}
          </Badge>
          <Badge tone={role.available ? "success" : "secondary"} className="shrink-0">
            {role.available ? `${role.prompt_chars.toLocaleString()} симв.` : "недоступна"}
          </Badge>
          <span className={cn("ml-auto text-xs text-muted-foreground", themedBody)}>
            {role.when}
          </span>
        </div>
        <p className={cn("text-sm text-muted-foreground", themedBody)}>{role.purpose}</p>
        <code className="text-xs text-text-tertiary">
          {role.module}.{role.attr}
        </code>
        {hasPrompt && (
          <>
            <Button
              ghost
              size="sm"
              className="w-fit gap-1 px-2 text-xs text-text-secondary"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
            >
              {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              {open ? "скрыть системный промпт" : "показать системный промпт"}
            </Button>
            {open && (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-sm bg-muted p-3 text-xs">
                {role.system_prompt}
              </pre>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function SurfaceCard({ surface }: { surface: EngineRoomSurface }) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{surface.title}</span>
          <code className="rounded-sm bg-muted px-1.5 py-0.5 text-xs text-text-tertiary">
            {surface.route}
          </code>
          <Badge tone="outline" className="ml-auto shrink-0 text-xs">
            {surface.owner_task}
          </Badge>
        </div>
        <p className={cn("text-sm text-muted-foreground", themedBody)}>{surface.purpose}</p>
      </CardContent>
    </Card>
  );
}

function SubstrateCard({ row }: { row: EngineRoomSubstrate }) {
  return (
    <Card className={cn(!row.present && "opacity-60")}>
      <CardContent className="flex flex-col gap-1.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{row.title}</span>
          <code className="rounded-sm bg-muted px-1.5 py-0.5 text-xs text-text-tertiary">
            {row.table ? `${row.db}:${row.table}` : row.db}
          </code>
          <Badge tone={row.present ? "success" : "secondary"} className="ml-auto shrink-0">
            {row.present ? "есть данные" : "нет данных"}
          </Badge>
        </div>
        <p className={cn("text-sm text-muted-foreground", themedBody)}>{row.purpose}</p>
      </CardContent>
    </Card>
  );
}

export default function EngineRoomPage() {
  const [model, setModel] = useState<EngineRoomModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getEngineRoom()
      .then((data) => {
        setModel(data);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-8 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Cog className="h-5 w-5 text-text-secondary" />
          <H2>Под капотом</H2>
        </div>
        <Button ghost size="sm" className="gap-1.5" onClick={load}>
          <RotateCw className="h-3.5 w-3.5" />
          обновить
        </Button>
      </div>
      <p className={cn("text-sm text-muted-foreground", themedBody)}>
        Прозрачное окно во внутренности приложения: те же живые данные, что и на
        пользовательской стороне, только видно машинерию — системные промпты
        мета-ролей, каталог служебных поверхностей и телеметрический субстрат.
      </p>

      {error && (
        <div className={cn("text-sm text-destructive", themedBody)}>
          Не удалось загрузить модель движка: {error}
        </div>
      )}

      {model && (
        <>
          <section className="flex flex-col gap-3">
            <SectionHeading
              title="Роли"
              hint="Системные промпты, под которыми работают автономные мета-роли (читаются живьём из исходников)."
            />
            {model.roles.map((r) => (
              <RoleCard key={r.key} role={r} />
            ))}
          </section>

          <section className="flex flex-col gap-3">
            <SectionHeading
              title="Поверхности"
              hint="Каталог служебных панелей движка — что где живёт и какая задача им владеет."
            />
            {model.surfaces.map((s) => (
              <SurfaceCard key={s.key} surface={s} />
            ))}
          </section>

          <section className="flex flex-col gap-3">
            <SectionHeading
              title="Субстрат"
              hint="Нативные источники телеметрии; наличие проверено по живым хранилищам."
            />
            {model.substrate.map((row) => (
              <SubstrateCard key={row.key} row={row} />
            ))}
          </section>
        </>
      )}
    </div>
  );
}
