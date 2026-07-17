import { useCallback, useEffect, useState } from "react";
import {
  AUTO_UPDATE_STORAGE_KEY,
  readAutoUpdate,
  writeAutoUpdate,
} from "@/lib/auto-update";

// One source of truth per tab. Every ``useAutoUpdate`` instance registers
// here so a flip in the header toggle is seen instantly by the lists that
// gate their polling on it — without threading the value through context.
// The browser ``storage`` event covers the cross-tab case (it only fires in
// *other* tabs, hence the in-tab set as well).
const listeners = new Set<(on: boolean) => void>();

function broadcast(on: boolean): void {
  for (const notify of listeners) notify(on);
}

function currentStorage(): Storage | null {
  return typeof window === "undefined" ? null : window.localStorage;
}

/**
 * Read/write the auto-update preference (default ON) with instant in-tab and
 * cross-tab synchronisation. Consumers gate their self-refresh on ``enabled``
 * (e.g. a task list freezes when off); the header toggle drives ``setEnabled``.
 * The chat surface intentionally never uses this — it stays always-live.
 */
export function useAutoUpdate(): {
  enabled: boolean;
  setEnabled: (on: boolean) => void;
} {
  const [enabled, setStateEnabled] = useState(() =>
    readAutoUpdate(currentStorage()),
  );

  useEffect(() => {
    const onLocal = (on: boolean) => setStateEnabled(on);
    listeners.add(onLocal);

    const onStorage = (event: StorageEvent) => {
      if (event.key === AUTO_UPDATE_STORAGE_KEY) {
        setStateEnabled(readAutoUpdate(currentStorage()));
      }
    };
    window.addEventListener("storage", onStorage);

    return () => {
      listeners.delete(onLocal);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setEnabled = useCallback((on: boolean) => {
    writeAutoUpdate(currentStorage(), on);
    broadcast(on);
  }, []);

  return { enabled, setEnabled };
}
