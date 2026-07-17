import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { api } from "@/lib/api";
import type { StatusResponse } from "@/lib/api";
import { en } from "@/i18n/en";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Yellow "restart pending" dot by the logo.
 *
 * One indicator for two sources of stale engine code: a self-redeploy merge
 * and the user's own vibe-code commits. Both only take effect after the
 * gateway restarts (a long-lived process freezes its modules at boot), so the
 * backend surfaces a single ``status.pending_reload`` signal (gateway boot rev
 * vs. disk rev) and this badge offers a one-click restart.
 *
 * "Restart" here means the engine/gateway, NOT a page reload: running tasks are
 * detached workers that survive it (task t_42950fee), which the confirm copy
 * spells out. Interactive default is notify-with-a-button, never auto-restart —
 * the user picks the moment.
 */
export function PendingReloadBadge({
  status,
}: {
  status: StatusResponse | null;
}) {
  const { t } = useI18n();
  // Untranslated locales fall back to the English copy (i18n has no per-key
  // fallback), so `pendingReload` is always defined here.
  const copy = t.app.pendingReload ?? en.app.pendingReload!;
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // Only meaningful while the engine is actually up — a stopped gateway has no
  // "pending restart", and its status file may still carry a stale value.
  if (!status?.gateway_running || !status.pending_reload) return null;

  const restart = async () => {
    setBusy(true);
    try {
      await api.restartGateway();
    } catch {
      // The restart is fire-and-forget; the 10s status poll reflects the
      // outcome (badge clears once the new gateway reports a matching rev).
    }
    setBusy(false);
    setOpen(false);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={copy.tooltip}
        aria-label={copy.tooltip}
        className={cn(
          "inline-flex h-2.5 w-2.5 shrink-0 rounded-full",
          "bg-warning ring-2 ring-warning/30",
          "animate-pulse transition-transform hover:scale-110",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning/60",
        )}
      />
      <ConfirmDialog
        open={open}
        loading={busy}
        title={copy.confirmTitle}
        description={copy.confirmBody}
        confirmLabel={copy.confirmButton}
        onConfirm={restart}
        onCancel={() => setOpen(false)}
      />
    </>
  );
}
