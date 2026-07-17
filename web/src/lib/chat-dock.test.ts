import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  CHAT_DOCK_OPEN_KEY,
  chatDockMode,
  loadChatDockOpen,
  persistChatDockOpen,
} from "./chat-dock";

describe("chatDockMode", () => {
  it("is hidden when unavailable, regardless of open/narrow", () => {
    expect(chatDockMode({ available: false, open: true, narrow: false })).toBe("hidden");
    expect(chatDockMode({ available: false, open: true, narrow: true })).toBe("hidden");
  });

  it("is hidden when available but closed", () => {
    expect(chatDockMode({ available: true, open: false, narrow: false })).toBe("hidden");
    expect(chatDockMode({ available: true, open: false, narrow: true })).toBe("hidden");
  });

  it("pushes on a wide window when open", () => {
    expect(chatDockMode({ available: true, open: true, narrow: false })).toBe("push");
  });

  it("overlays on a narrow window when open", () => {
    expect(chatDockMode({ available: true, open: true, narrow: true })).toBe("overlay");
  });
});

// The lib runs in the `node` vitest env (no DOM), so stub a minimal
// in-memory localStorage the way the other lib tests stub globals.
function fakeLocalStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    removeItem: (k: string) => store.delete(k),
    setItem: (k: string, v: string) => store.set(k, String(v)),
  } as Storage;
}

describe("persisted preference", () => {
  const originalLocalStorage = globalThis.localStorage;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: fakeLocalStorage(),
      configurable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: originalLocalStorage,
      configurable: true,
    });
  });

  it("defaults to closed with no stored value", () => {
    expect(loadChatDockOpen()).toBe(false);
  });

  it("round-trips through localStorage", () => {
    persistChatDockOpen(true);
    expect(localStorage.getItem(CHAT_DOCK_OPEN_KEY)).toBe("true");
    expect(loadChatDockOpen()).toBe(true);

    persistChatDockOpen(false);
    expect(loadChatDockOpen()).toBe(false);
  });

  it('treats any non-"true" value as closed', () => {
    localStorage.setItem(CHAT_DOCK_OPEN_KEY, "1");
    expect(loadChatDockOpen()).toBe(false);
  });

  it("degrades gracefully when localStorage throws", () => {
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      },
      configurable: true,
    });
    expect(loadChatDockOpen()).toBe(false);
    expect(() => persistChatDockOpen(true)).not.toThrow();
  });
});
