import { describe, expect, it, vi } from "vitest";

import type { ConnectionState } from "@/lib/gatewayClient";
import {
  computeBackoffDelay,
  DEFAULT_BACKOFF,
  GatewayReconnectLoop,
  LAST_RELOAD_AT_KEY,
  PRESERVED_ROUTE_KEY,
  reloadRecentlyTriggered,
  savePreservedRoute,
  shouldForceReload,
  takePreservedRoute,
  type ReconnectableGateway,
} from "./gateway-reconnect";

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("computeBackoffDelay", () => {
  it("grows exponentially from the base delay", () => {
    expect(computeBackoffDelay(1)).toBe(500);
    expect(computeBackoffDelay(2)).toBe(1000);
    expect(computeBackoffDelay(3)).toBe(2000);
    expect(computeBackoffDelay(4)).toBe(4000);
    expect(computeBackoffDelay(5)).toBe(8000);
  });

  it("caps at maxDelayMs", () => {
    expect(computeBackoffDelay(10)).toBe(DEFAULT_BACKOFF.maxDelayMs);
    expect(
      computeBackoffDelay(3, { baseDelayMs: 100, factor: 10, maxDelayMs: 500 }),
    ).toBe(500);
  });

  it("treats attempt 0/1 as the base delay", () => {
    expect(computeBackoffDelay(0)).toBe(500);
    expect(computeBackoffDelay(1)).toBe(500);
  });
});

describe("shouldForceReload", () => {
  const base = { maxAttempts: 5, windowMs: 30_000 };

  it("reloads once the attempt budget is exhausted", () => {
    expect(shouldForceReload({ ...base, attempts: 5, elapsedMs: 0 })).toBe(true);
    expect(shouldForceReload({ ...base, attempts: 4, elapsedMs: 0 })).toBe(false);
  });

  it("reloads once the time window is exhausted", () => {
    expect(shouldForceReload({ ...base, attempts: 2, elapsedMs: 30_000 })).toBe(
      true,
    );
    expect(shouldForceReload({ ...base, attempts: 2, elapsedMs: 29_999 })).toBe(
      false,
    );
  });
});

describe("route preservation", () => {
  const makeStorage = () => {
    const map = new Map<string, string>();
    return {
      getItem: (k: string) => map.get(k) ?? null,
      setItem: (k: string, v: string) => void map.set(k, v),
      removeItem: (k: string) => void map.delete(k),
      _map: map,
    };
  };

  it("saves and takes the preserved route exactly once", () => {
    const storage = makeStorage();
    savePreservedRoute(storage, "/chat?c=main#x");
    expect(storage._map.get(PRESERVED_ROUTE_KEY)).toBe("/chat?c=main#x");
    expect(takePreservedRoute(storage)).toBe("/chat?c=main#x");
    // Cleared after being taken.
    expect(takePreservedRoute(storage)).toBeNull();
  });

  it("tolerates a null storage", () => {
    expect(() => savePreservedRoute(null, "/x")).not.toThrow();
    expect(takePreservedRoute(null)).toBeNull();
    expect(reloadRecentlyTriggered(null, 0)).toBe(false);
  });

  it("detects a recent fallback reload within the cooldown", () => {
    const storage = makeStorage();
    storage.setItem(LAST_RELOAD_AT_KEY, "1000");
    expect(reloadRecentlyTriggered(storage, 1000 + 59_000)).toBe(true);
    expect(reloadRecentlyTriggered(storage, 1000 + 60_000)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Controller — driven with a mock gateway + fake clock/scheduler
// ---------------------------------------------------------------------------

class MockGateway implements ReconnectableGateway {
  state: ConnectionState = "idle";
  connectCalls = 0;
  private handlers = new Set<(s: ConnectionState) => void>();

  connect(): Promise<void> {
    this.connectCalls += 1;
    this.setState("connecting");
    return Promise.resolve();
  }

  onState(handler: (s: ConnectionState) => void): () => void {
    this.handlers.add(handler);
    handler(this.state);
    return () => this.handlers.delete(handler);
  }

  get connectionState(): ConnectionState {
    return this.state;
  }

  setState(s: ConnectionState): void {
    this.state = s;
    for (const h of [...this.handlers]) {
      h(s);
    }
  }

  /** A live socket opens. */
  open(): void {
    this.setState("open");
  }

  /** A connect attempt fails — emits error THEN close, like a real socket. */
  fail(): void {
    this.setState("error");
    this.setState("closed");
  }

  /** A previously open socket drops — a lone close event. */
  drop(): void {
    this.setState("closed");
  }
}

interface Harness {
  gateway: MockGateway;
  loop: GatewayReconnectLoop;
  reload: ReturnType<typeof vi.fn>;
  storage: {
    getItem: (k: string) => string | null;
    setItem: (k: string, v: string) => void;
    removeItem: (k: string) => void;
    _map: Map<string, string>;
  };
  onOpen: ReturnType<typeof vi.fn>;
  onError: ReturnType<typeof vi.fn>;
  onReconnecting: ReturnType<typeof vi.fn>;
  setClock: (t: number) => void;
  flushTimer: () => void;
  pendingDelays: () => number[];
}

const tick = () => new Promise<void>((r) => setTimeout(r, 0));

function makeHarness(
  opts: {
    onOpenImpl?: (ctx: { reconnected: boolean }) => void | Promise<void>;
  } = {},
): Harness {
  const gateway = new MockGateway();
  const map = new Map<string, string>();
  const storage = {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    _map: map,
  };
  let clock = 0;
  const timers: { id: number; fn: () => void; ms: number }[] = [];
  let nextId = 1;
  const reload = vi.fn();
  const onOpen = vi.fn(opts.onOpenImpl ?? (() => {}));
  const onError = vi.fn();
  const onReconnecting = vi.fn();

  const loop = new GatewayReconnectLoop({
    gateway,
    onOpen,
    onError,
    onReconnecting,
    reload,
    storage,
    currentRoute: () => "/chat?c=main",
    now: () => clock,
    schedule: (fn, ms) => {
      const id = nextId++;
      timers.push({ id, fn, ms });
      return id as unknown as ReturnType<typeof setTimeout>;
    },
    cancel: (h) => {
      const i = timers.findIndex((t) => t.id === (h as unknown as number));
      if (i >= 0) timers.splice(i, 1);
    },
  });

  return {
    gateway,
    loop,
    reload,
    storage,
    onOpen,
    onError,
    onReconnecting,
    setClock: (t) => {
      clock = t;
    },
    flushTimer: () => {
      const t = timers.shift();
      if (t) t.fn();
    },
    pendingDelays: () => timers.map((t) => t.ms),
  };
}

describe("GatewayReconnectLoop", () => {
  it("syncs on first connect without marking it a reconnect", async () => {
    const h = makeHarness();
    h.loop.start();
    expect(h.gateway.connectionState).toBe("connecting");
    h.gateway.open();
    await tick();
    expect(h.onOpen).toHaveBeenCalledTimes(1);
    expect(h.onOpen).toHaveBeenCalledWith({ reconnected: false });
    expect(h.reload).not.toHaveBeenCalled();
    expect(h.loop.loopState).toBe("open");
  });

  it("recovers a dropped socket and refetches state as a reconnect", async () => {
    const h = makeHarness();
    h.loop.start();
    h.gateway.open();
    await tick();
    h.onOpen.mockClear();

    h.gateway.drop();
    expect(h.onReconnecting).toHaveBeenCalledWith({ attempt: 1, delayMs: 500 });
    expect(h.loop.loopState).toBe("reconnecting");

    h.flushTimer(); // fires the scheduled reconnect → connect()
    h.gateway.open();
    await tick();
    expect(h.onOpen).toHaveBeenCalledWith({ reconnected: true });
    expect(h.reload).not.toHaveBeenCalled();
  });

  it("counts a failed connect (error+close) as a single attempt", async () => {
    const h = makeHarness();
    h.loop.start();
    h.gateway.fail(); // error THEN close
    expect(h.onReconnecting).toHaveBeenCalledTimes(1);
    expect(h.onReconnecting).toHaveBeenLastCalledWith({ attempt: 1, delayMs: 500 });

    h.flushTimer();
    h.gateway.fail();
    expect(h.onReconnecting).toHaveBeenCalledTimes(2);
    expect(h.onReconnecting).toHaveBeenLastCalledWith({ attempt: 2, delayMs: 1000 });
  });

  it("falls back to reload after the attempt budget, preserving the route", async () => {
    const h = makeHarness();
    h.loop.start();
    // 5 consecutive failures at the same instant → attempts hits maxAttempts.
    for (let i = 0; i < 4; i++) {
      h.gateway.fail();
      h.flushTimer();
    }
    h.gateway.fail(); // 5th
    expect(h.reload).toHaveBeenCalledTimes(1);
    expect(h.loop.loopState).toBe("reloading");
    expect(h.storage._map.get(PRESERVED_ROUTE_KEY)).toBe("/chat?c=main");
    expect(h.storage._map.get(LAST_RELOAD_AT_KEY)).toBe("0");
    // No further reconnect scheduled after reload.
    expect(h.pendingDelays()).toHaveLength(0);
  });

  it("falls back to reload once the time window is exhausted", async () => {
    const h = makeHarness();
    h.loop.start();
    h.gateway.fail(); // attempt 1 at t=0
    h.flushTimer();
    h.setClock(30_000);
    h.gateway.fail(); // attempt 2, elapsed 30s → reload
    expect(h.reload).toHaveBeenCalledTimes(1);
  });

  it("reloads when a post-reconnect state sync fails", async () => {
    let calls = 0;
    const h = makeHarness({
      onOpenImpl: ({ reconnected }) => {
        calls += 1;
        if (reconnected) {
          return Promise.reject(new Error("state fetch failed"));
        }
        return Promise.resolve();
      },
    });
    h.loop.start();
    h.gateway.open();
    await tick();
    expect(h.reload).not.toHaveBeenCalled();

    h.gateway.drop();
    h.flushTimer();
    h.gateway.open();
    await tick();
    expect(calls).toBe(2);
    expect(h.reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload when the FIRST connect's sync fails", async () => {
    const h = makeHarness({
      onOpenImpl: () => Promise.reject(new Error("boom")),
    });
    h.loop.start();
    h.gateway.open();
    await tick();
    expect(h.reload).not.toHaveBeenCalled();
    expect(h.onError).toHaveBeenCalledTimes(1);
    expect(h.loop.loopState).toBe("open");
  });

  it("suppresses a repeat reload within the cooldown and keeps retrying", async () => {
    const h = makeHarness();
    h.setClock(5_000);
    h.storage.setItem(LAST_RELOAD_AT_KEY, "0"); // reloaded 5s ago
    h.loop.start();
    for (let i = 0; i < 4; i++) {
      h.gateway.fail();
      h.flushTimer();
    }
    h.gateway.fail(); // would reload, but within cooldown
    expect(h.reload).not.toHaveBeenCalled();
    // A fresh retry is scheduled instead of trapping the user.
    expect(h.pendingDelays()).toHaveLength(1);
    expect(h.loop.loopState).toBe("reconnecting");
  });

  it("stops cleanly: cancels timers, unsubscribes, no further reconnects", async () => {
    const h = makeHarness();
    h.loop.start();
    h.gateway.fail(); // schedules a reconnect
    expect(h.pendingDelays()).toHaveLength(1);
    const callsBefore = h.gateway.connectCalls;

    h.loop.stop();
    expect(h.pendingDelays()).toHaveLength(0);
    expect(h.loop.loopState).toBe("idle");

    // State changes after stop are ignored (handler unsubscribed).
    h.gateway.open();
    await tick();
    expect(h.onOpen).not.toHaveBeenCalled();
    expect(h.gateway.connectCalls).toBe(callsBefore);
  });

  it("schedules backoff with the documented delay sequence", () => {
    const h = makeHarness();
    h.loop.start();
    const seen: number[] = [];
    for (let i = 0; i < 4; i++) {
      h.gateway.fail();
      seen.push(h.pendingDelays()[0]);
      h.flushTimer();
    }
    expect(seen).toEqual([500, 1000, 2000, 4000]);
  });
});
