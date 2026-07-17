/**
 * Collapsible right-side chat dock — layout state helpers.
 *
 * The dashboard keeps a single persistent ChatPage instance mounted (so the
 * PTY session survives navigation). On the `/chat` route it paints full-page;
 * on every other route it can also be summoned as a collapsible dock on the
 * right edge, coexisting with the page underneath (notably the Kanban board).
 *
 * The dock is adaptive by window width:
 *  - wide window  → "push"    : the dock takes its own column and the page
 *                               content is padded aside so both stay usable.
 *  - narrow window → "overlay": the dock floats over the right of the page so
 *                               the content keeps its full width — no need to
 *                               widen the window to read the board.
 *
 * These are pure functions so the branch logic is unit-tested without a DOM.
 */

/** localStorage key persisting the operator's open/closed preference. */
export const CHAT_DOCK_OPEN_KEY = "hermes-chat-dock-open";

/** Dock width in CSS pixels (push column width and overlay panel width). */
export const CHAT_DOCK_WIDTH = 400;

/**
 * Below this viewport width the dock switches from push to overlay. Roughly a
 * half-screen split of a common 2560px display; below it the board no longer
 * has room to give up a 400px column, so the dock floats instead of shrinking.
 */
export const CHAT_DOCK_PUSH_MIN_WIDTH = 1280;

export type ChatDockMode = "hidden" | "push" | "overlay";

/**
 * Resolve how the dock should render on a non-chat route.
 *
 * `available` folds in the gating conditions (embedded chat on, no plugin
 * override, not already on the full-page `/chat` route). When the dock is
 * unavailable or closed it is hidden (but the ChatPage stays mounted, just
 * `display:none`, to keep the session alive).
 */
export function chatDockMode(opts: {
  available: boolean;
  open: boolean;
  narrow: boolean;
}): ChatDockMode {
  if (!opts.available || !opts.open) return "hidden";
  return opts.narrow ? "overlay" : "push";
}

/** Read the persisted open/closed preference; defaults to closed. */
export function loadChatDockOpen(): boolean {
  try {
    return localStorage.getItem(CHAT_DOCK_OPEN_KEY) === "true";
  } catch {
    return false;
  }
}

/** Persist the open/closed preference; tolerates unavailable storage. */
export function persistChatDockOpen(open: boolean): void {
  try {
    localStorage.setItem(CHAT_DOCK_OPEN_KEY, String(open));
  } catch {
    /* localStorage may be unavailable in private browsing */
  }
}
