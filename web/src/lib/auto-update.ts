/**
 * Auto-update policy for the dashboard's self-refreshing surfaces
 * (operator wish t_f0a1b527, layered on the WS-live feed + React diff).
 *
 * Two concerns live here, both pure so they unit-test without a DOM:
 *
 *  1. **The toggle** — a persisted, default-ON preference. In the browser
 *     the user can always press F5; the standalone (Tauri) app has no F5, so
 *     the toggle is the only way to freeze a list while interacting with it.
 *     Kept default-ON because self-refresh is the point; turning it off is
 *     the rare escape hatch, not the norm.
 *
 *  2. **Diff-by-visibility** — a list should silently re-fetch only when a
 *     field the UI actually renders has changed. A WS/poll event that moves
 *     an *invisible* field (token counts, timestamps we don't show, internal
 *     ids) must not trigger a reload: that only flickers data the user can't
 *     see. ``visibleListSignature`` projects a list down to its rendered
 *     fields; ``shouldRefreshVisible`` compares two such projections.
 *
 * The chat surface deliberately does NOT consult any of this — it is an
 * always-live WebSocket stream and is never force-refreshed (it would tear
 * an in-flight interaction). Only the (near view-only) task/session lists
 * opt in. See ``useAutoUpdate`` for the React binding and ``SessionsPage``
 * for the reference wiring.
 */

export const AUTO_UPDATE_STORAGE_KEY = "hermes.autoUpdate";

/** Default ON: surfaces refresh themselves unless the user opts out. */
export const AUTO_UPDATE_DEFAULT = true;

type ReadableStorage = Pick<Storage, "getItem"> | null | undefined;
type WritableStorage = Pick<Storage, "setItem"> | null | undefined;

/**
 * Read the persisted auto-update preference, defaulting to ON when unset,
 * when there is no storage (SSR / private mode), or on any access error.
 * Only the explicit strings ``"0"`` / ``"false"`` mean off.
 */
export function readAutoUpdate(storage: ReadableStorage): boolean {
  if (!storage) return AUTO_UPDATE_DEFAULT;
  try {
    const raw = storage.getItem(AUTO_UPDATE_STORAGE_KEY);
    if (raw === null) return AUTO_UPDATE_DEFAULT;
    return raw !== "0" && raw !== "false";
  } catch {
    return AUTO_UPDATE_DEFAULT;
  }
}

/** Persist the preference. Best-effort: storage errors are swallowed. */
export function writeAutoUpdate(storage: WritableStorage, on: boolean): void {
  try {
    storage?.setItem(AUTO_UPDATE_STORAGE_KEY, on ? "1" : "0");
  } catch {
    // private mode / quota — the toggle still works for this session.
  }
}

/**
 * Project one row down to the fields the UI renders, as a stable string.
 * Missing fields normalise to ``null`` so an absent vs. explicitly-null
 * value never reads as a change.
 */
export function visibleSignature<T>(
  row: T,
  fields: readonly (keyof T)[],
): string {
  return JSON.stringify(fields.map((f) => row[f] ?? null));
}

/**
 * Signature of a whole list's *visible* projection, order-sensitive so a
 * reorder (rows visibly moving) counts as a change while an invisible-field
 * update on any row does not. ``limit`` caps how deep to look — a change
 * below the fold the user can't see should not force a reload either.
 */
export function visibleListSignature<T>(
  rows: readonly T[],
  fields: readonly (keyof T)[],
  limit: number = rows.length,
): string {
  return rows
    .slice(0, limit)
    .map((row) => visibleSignature(row, fields))
    .join("|");
}

/**
 * Whether a silent refresh should fire, given the previous and current
 * visible signatures. ``prev === null`` (first poll, no baseline yet)
 * returns ``false`` so mounting never triggers a redundant reload.
 */
export function shouldRefreshVisible(prev: string | null, next: string): boolean {
  return prev !== null && prev !== next;
}
