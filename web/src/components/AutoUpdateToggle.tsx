import { Pause, RefreshCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import { useAutoUpdate } from "@/hooks/useAutoUpdate";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Header control that flips the dashboard's auto-update preference (default
 * ON). In the browser F5 is always available; the standalone (Tauri) app has
 * no F5, so this toggle is the way to freeze the self-refreshing task list
 * while interacting with it. The chat surface is unaffected — it is always
 * live regardless of this switch. Sits next to the theme/language switchers.
 */
export function AutoUpdateToggle({ collapsed = false }: AutoUpdateToggleProps) {
  const { enabled, setEnabled } = useAutoUpdate();
  const { t } = useI18n();

  const label = enabled
    ? t.autoUpdate?.on ?? "Auto-update on"
    : t.autoUpdate?.off ?? "Auto-update off";
  const Icon = enabled ? RefreshCw : Pause;

  return (
    <Button
      ghost
      size={collapsed ? "icon" : undefined}
      onClick={() => setEnabled(!enabled)}
      aria-pressed={enabled}
      title={t.autoUpdate?.hint ?? label}
      aria-label={label}
      className={cn(
        collapsed
          ? "text-text-secondary hover:text-foreground hover:bg-transparent"
          : "px-2 py-1 normal-case tracking-normal font-normal text-xs text-text-secondary hover:text-foreground",
      )}
    >
      <span className="inline-flex items-center gap-1.5">
        <Icon className={cn("h-3.5 w-3.5", enabled && "text-success")} />

        {!collapsed && (
          <Typography className="hidden sm:inline text-display tracking-wide text-xs">
            {label}
          </Typography>
        )}
      </span>
    </Button>
  );
}

interface AutoUpdateToggleProps {
  collapsed?: boolean;
}
