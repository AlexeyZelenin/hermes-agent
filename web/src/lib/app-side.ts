/**
 * Two sides of the dashboard — the user side and the "engine room" (под капотом).
 *
 * Product idea (t_41eb5967, building on the engine-room model in
 * `hermes_cli/engine_room.py`): the app has two faces over the *same* live
 * state. (1) The **user** side — day-to-day work: chat, sessions, files, the
 * board. (2) The **engine room** — the machinery: role prompts, crons, limits,
 * the decision ledger, diagnostics, telemetry, configuration. The organizing
 * principle is that *all meta lives on the engine-room side* so it never
 * clutters the user side; both sides read the same stores, so a change on one
 * is visible on the other. The side is not app state we store separately — it
 * is *derived* from the current route (see {@link sideForPath}), so the toggle
 * and the URL can never disagree.
 *
 * These are pure functions so the partition + persistence branch logic is
 * unit-tested without a DOM or a router.
 */

export type AppSide = "user" | "engine";

/** Landing route for each side (used when a side has no remembered route). */
export const APP_SIDE_HOME: Record<AppSide, string> = {
  user: "/sessions",
  engine: "/engine-room",
};

/**
 * Built-in routes that live on the engine-room side — everything meta:
 * observability, wiring, configuration. A path not listed here (including every
 * plugin tab, e.g. the Kanban board) defaults to the user side, keeping that
 * side uncluttered. Matched by exact path or `path/` prefix so nested routes
 * (e.g. `/profiles/new`) inherit their parent's side.
 */
export const ENGINE_SIDE_PATHS: readonly string[] = [
  "/engine-room",
  "/analytics",
  "/models",
  "/logs",
  "/cron",
  "/regular",
  "/problems",
  "/pacing",
  "/mcp",
  "/channels",
  "/webhooks",
  "/pairing",
  "/profiles",
  "/config",
  "/env",
  "/system",
  "/skills",
  "/plugins",
];

/** Which side a route belongs to. Unknown/plugin routes are user-side. */
export function sideForPath(pathname: string): AppSide {
  for (const p of ENGINE_SIDE_PATHS) {
    if (pathname === p || pathname.startsWith(p + "/")) return "engine";
  }
  return "user";
}

/** Keep only the nav items that belong to `side`, preserving order. */
export function partitionNavBySide<T extends { path: string }>(
  items: T[],
  side: AppSide,
): T[] {
  return items.filter((item) => sideForPath(item.path) === side);
}

/** The other side — for a two-position toggle. */
export function otherSide(side: AppSide): AppSide {
  return side === "user" ? "engine" : "user";
}

/** localStorage key remembering the last route visited on each side. */
export const APP_SIDE_LAST_PATH_KEY = "hermes-app-side-last-path";

/**
 * Read the last-visited route per side so flipping the toggle restores the
 * context you left rather than always dumping you on the side's home. Defaults
 * to each side's home and tolerates unavailable/corrupt storage.
 */
export function loadLastPaths(): Record<AppSide, string> {
  const fallback: Record<AppSide, string> = {
    user: APP_SIDE_HOME.user,
    engine: APP_SIDE_HOME.engine,
  };
  try {
    const raw = localStorage.getItem(APP_SIDE_LAST_PATH_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<Record<AppSide, unknown>>;
    return {
      user: typeof parsed.user === "string" ? parsed.user : fallback.user,
      engine: typeof parsed.engine === "string" ? parsed.engine : fallback.engine,
    };
  } catch {
    return fallback;
  }
}

/** Persist the last-visited route per side; tolerates unavailable storage. */
export function persistLastPaths(paths: Record<AppSide, string>): void {
  try {
    localStorage.setItem(APP_SIDE_LAST_PATH_KEY, JSON.stringify(paths));
  } catch {
    /* localStorage may be unavailable in private browsing */
  }
}
