/**
 * Deterministic, dependency-injected reconnect loop for a JSON-RPC gateway
 * WebSocket (see {@link GatewayClient}).
 *
 * The transport in `@hermes/shared` only *reports* connection state — it never
 * retries on its own. This controller drives the retry policy on top of it:
 *
 *   - exponential backoff between reconnect attempts,
 *   - a state-sync hook that fires on every successful (re)open so the caller
 *     can refetch whatever the socket drop may have staled,
 *   - a full-page reload fallback when the socket can't recover within a
 *     bounded number of attempts / time window, or when a post-reconnect state
 *     sync fails — with the current route preserved across the reload.
 *
 * Every side effect (connect, timers, clock, reload, storage, location) is
 * injectable so the whole thing is testable under a Node test environment with
 * no browser globals. The pure helpers below carry the policy math and are
 * exported so they can be unit-tested in isolation.
 */

import type { ConnectionState } from "@/lib/gatewayClient";

/** Minimal surface this controller needs from a gateway client. */
export interface ReconnectableGateway {
  connect(token?: string): Promise<void>;
  onState(handler: (state: ConnectionState) => void): () => void;
  readonly connectionState: ConnectionState;
}

export interface BackoffOptions {
  baseDelayMs: number;
  maxDelayMs: number;
  factor: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = {
  baseDelayMs: 500,
  factor: 2,
  maxDelayMs: 10_000,
};

/** Attempts allowed inside {@link RELOAD_WINDOW_MS} before falling back. */
export const DEFAULT_MAX_ATTEMPTS = 5;
/** Reload if reconnection hasn't succeeded within this window. */
export const RELOAD_WINDOW_MS = 30_000;
/**
 * After a fallback reload we record a timestamp; a fresh page that still can't
 * connect must not immediately reload again or the user is trapped in a reload
 * loop while the gateway is down. Suppress a second fallback reload inside this
 * cooldown and stay in the reconnecting banner instead.
 */
export const RELOAD_COOLDOWN_MS = 60_000;

export const PRESERVED_ROUTE_KEY = "hermes.reconnectRoute";
export const LAST_RELOAD_AT_KEY = "hermes.reconnectReloadAt";

/**
 * Backoff delay before the Nth attempt (1-based): `base * factor^(n-1)`, capped
 * at `maxDelayMs`. Deterministic — no jitter — so tests and the retry budget
 * are exactly predictable.
 */
export function computeBackoffDelay(
  attempt: number,
  { baseDelayMs, maxDelayMs, factor }: BackoffOptions = DEFAULT_BACKOFF,
): number {
  if (attempt <= 1) {
    return Math.min(baseDelayMs, maxDelayMs);
  }
  const raw = baseDelayMs * factor ** (attempt - 1);
  return Math.min(raw, maxDelayMs);
}

export interface ReloadDecisionInput {
  attempts: number;
  elapsedMs: number;
  maxAttempts: number;
  windowMs: number;
}

/**
 * Fall back to a reload once the socket has failed to recover after
 * `maxAttempts` tries *or* once `windowMs` has elapsed since the first failure.
 * Either bound guarantees we stop retrying forever and hand the user a clean
 * page rather than a wedged one.
 */
export function shouldForceReload({
  attempts,
  elapsedMs,
  maxAttempts,
  windowMs,
}: ReloadDecisionInput): boolean {
  return attempts >= maxAttempts || elapsedMs >= windowMs;
}

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** Persist the current route so it can be restored after a fallback reload. */
export function savePreservedRoute(
  storage: StorageLike | null | undefined,
  route: string,
): void {
  try {
    storage?.setItem(PRESERVED_ROUTE_KEY, route);
  } catch {
    // Storage can throw (private mode / quota). Route preservation is
    // best-effort; a hard reload still lands on the same URL path.
  }
}

/** Read and clear a route preserved by a prior fallback reload, if any. */
export function takePreservedRoute(
  storage: StorageLike | null | undefined,
): string | null {
  try {
    const route = storage?.getItem(PRESERVED_ROUTE_KEY) ?? null;
    if (route !== null) {
      storage?.removeItem(PRESERVED_ROUTE_KEY);
    }
    return route;
  } catch {
    return null;
  }
}

/**
 * True when a fallback reload happened within {@link RELOAD_COOLDOWN_MS}; used
 * to break reload loops while the gateway stays down.
 */
export function reloadRecentlyTriggered(
  storage: StorageLike | null | undefined,
  now: number,
  cooldownMs = RELOAD_COOLDOWN_MS,
): boolean {
  try {
    const raw = storage?.getItem(LAST_RELOAD_AT_KEY);
    if (!raw) {
      return false;
    }
    const last = Number(raw);
    return Number.isFinite(last) && now - last < cooldownMs;
  } catch {
    return false;
  }
}

export interface OpenContext {
  /** True when this open recovered a prior drop (as opposed to first connect). */
  reconnected: boolean;
}

export interface ReconnectingContext {
  /** 1-based count of consecutive failed attempts. */
  attempt: number;
  /** Backoff delay scheduled before the next attempt. */
  delayMs: number;
}

export interface GatewayReconnectOptions {
  gateway: ReconnectableGateway;
  /**
   * Runs on every successful open. Refetch whatever the drop may have staled
   * (e.g. `/state`, `/subscriptions`). If it rejects *after a reconnect*, the
   * controller falls back to a reload; a rejection on the very first connect is
   * surfaced via {@link onError} without reloading (avoids reload loops when a
   * post-connect RPC is simply flaky).
   */
  onOpen?: (ctx: OpenContext) => void | Promise<void>;
  /** Fired when a drop is detected and a retry is scheduled. */
  onReconnecting?: (ctx: ReconnectingContext) => void;
  /** Raw connection-state passthrough for banner/badge rendering. */
  onState?: (state: ConnectionState) => void;
  /** Non-fatal failure of the first-connect state sync. */
  onError?: (error: Error) => void;

  backoff?: Partial<BackoffOptions>;
  maxAttempts?: number;
  windowMs?: number;
  reloadCooldownMs?: number;

  // --- injectable side effects (browser defaults resolved lazily) ---
  now?: () => number;
  schedule?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  cancel?: (handle: ReturnType<typeof setTimeout>) => void;
  reload?: () => void;
  storage?: StorageLike | null;
  currentRoute?: () => string;
}

type LoopState = "idle" | "connecting" | "open" | "reconnecting" | "reloading";

function defaultReload(): void {
  if (typeof window !== "undefined") {
    window.location.reload();
  }
}

function defaultRoute(): string {
  if (typeof window === "undefined") {
    return "";
  }
  const { pathname, search, hash } = window.location;
  return `${pathname}${search}${hash}`;
}

function defaultStorage(): StorageLike | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

/**
 * Orchestrates the reconnect lifecycle. Call {@link start} once the caller is
 * ready to (re)connect and {@link stop} on teardown. Safe to `start`/`stop`
 * repeatedly; all scheduled work is cancelled on stop.
 */
export class GatewayReconnectLoop {
  private readonly gateway: ReconnectableGateway;
  private readonly opts: GatewayReconnectOptions;
  private readonly backoff: BackoffOptions;
  private readonly maxAttempts: number;
  private readonly windowMs: number;
  private readonly reloadCooldownMs: number;
  private readonly now: () => number;
  private readonly schedule: NonNullable<GatewayReconnectOptions["schedule"]>;
  private readonly cancel: NonNullable<GatewayReconnectOptions["cancel"]>;
  private readonly reload: () => void;
  private readonly storage: StorageLike | null;
  private readonly currentRoute: () => string;

  private state: LoopState = "idle";
  private running = false;
  private attempts = 0;
  private firstFailureAt: number | null = null;
  private sawDrop = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private unsubscribe: (() => void) | null = null;
  private syncSeq = 0;
  // A single failed connect emits both 'error' and 'close'; count only the
  // first failure per attempt. Set true on each 'connecting'/'open' transition
  // (a live connection can still drop) and cleared by the first failure.
  private attemptInFlight = false;

  constructor(options: GatewayReconnectOptions) {
    this.gateway = options.gateway;
    this.opts = options;
    this.backoff = { ...DEFAULT_BACKOFF, ...options.backoff };
    this.maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
    this.windowMs = options.windowMs ?? RELOAD_WINDOW_MS;
    this.reloadCooldownMs = options.reloadCooldownMs ?? RELOAD_COOLDOWN_MS;
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? ((fn, ms) => setTimeout(fn, ms));
    this.cancel = options.cancel ?? ((h) => clearTimeout(h));
    this.reload = options.reload ?? defaultReload;
    this.storage =
      options.storage === undefined ? defaultStorage() : options.storage;
    this.currentRoute = options.currentRoute ?? defaultRoute;
  }

  get loopState(): LoopState {
    return this.state;
  }

  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    this.attempts = 0;
    this.firstFailureAt = null;
    this.sawDrop = false;
    this.state = "connecting";
    this.unsubscribe = this.gateway.onState((s) => this.handleState(s));
    void this.gateway.connect().catch(() => {
      // The 'error'/'closed' state transition drives retry; swallow the
      // rejection so it doesn't surface as an unhandled promise.
    });
  }

  stop(): void {
    this.running = false;
    this.syncSeq += 1;
    this.clearTimer();
    if (this.unsubscribe) {
      this.unsubscribe();
      this.unsubscribe = null;
    }
    if (this.state !== "reloading") {
      this.state = "idle";
    }
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      this.cancel(this.timer);
      this.timer = null;
    }
  }

  private handleState(state: ConnectionState): void {
    this.opts.onState?.(state);
    if (!this.running || this.state === "reloading") {
      return;
    }
    if (state === "connecting") {
      this.attemptInFlight = true;
    } else if (state === "open") {
      // Stay "in flight" while open so a later drop (a lone 'close' with no
      // preceding 'connecting') is still counted as a failure.
      this.attemptInFlight = true;
      this.handleOpen();
    } else if (state === "closed" || state === "error") {
      if (!this.attemptInFlight) {
        return;
      }
      this.attemptInFlight = false;
      this.handleFailure();
    }
  }

  private handleOpen(): void {
    this.clearTimer();
    const reconnected = this.sawDrop;
    this.attempts = 0;
    this.firstFailureAt = null;
    this.sawDrop = false;
    this.state = "open";
    void this.runSync(reconnected);
  }

  private async runSync(reconnected: boolean): Promise<void> {
    const seq = ++this.syncSeq;
    try {
      await this.opts.onOpen?.({ reconnected });
    } catch (error) {
      // A stop() or a newer open superseded this sync — drop its outcome.
      if (seq !== this.syncSeq || !this.running) {
        return;
      }
      const err = error instanceof Error ? error : new Error(String(error));
      // Post-reconnect state fetch failed: the socket is back but data is
      // unrecoverably stale — fall back to a clean reload. On the first
      // connect (or when a reload is suppressed to break a loop) just surface
      // the error; the socket itself is healthy.
      if (reconnected && this.attemptReload()) {
        return;
      }
      this.opts.onError?.(err);
    }
  }

  private handleFailure(): void {
    if (this.state === "reloading") {
      return;
    }
    this.clearTimer();
    this.sawDrop = true;
    this.attempts += 1;
    const now = this.now();
    if (this.firstFailureAt === null) {
      this.firstFailureAt = now;
    }
    const elapsedMs = now - this.firstFailureAt;
    if (
      shouldForceReload({
        attempts: this.attempts,
        elapsedMs,
        maxAttempts: this.maxAttempts,
        windowMs: this.windowMs,
      })
    ) {
      if (this.attemptReload()) {
        return;
      }
      // Reload suppressed to avoid a loop while the gateway is down. Open a
      // fresh window and keep retrying with the capped backoff.
      this.attempts = 1;
      this.firstFailureAt = now;
    }
    const delayMs = computeBackoffDelay(this.attempts, this.backoff);
    this.state = "reconnecting";
    this.opts.onReconnecting?.({ attempt: this.attempts, delayMs });
    this.timer = this.schedule(() => {
      this.timer = null;
      if (!this.running) {
        return;
      }
      void this.gateway.connect().catch(() => {});
    }, delayMs);
  }

  /**
   * Fall back to a full-page reload, preserving the current route. Returns
   * false (without reloading) when a reload happened within the cooldown, so
   * the caller keeps retrying instead of trapping the user in a reload loop.
   */
  private attemptReload(): boolean {
    if (reloadRecentlyTriggered(this.storage, this.now(), this.reloadCooldownMs)) {
      return false;
    }
    this.state = "reloading";
    this.clearTimer();
    savePreservedRoute(this.storage, this.currentRoute());
    try {
      this.storage?.setItem(LAST_RELOAD_AT_KEY, String(this.now()));
    } catch {
      // best-effort
    }
    this.reload();
    return true;
  }
}
