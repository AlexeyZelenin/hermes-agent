import { afterEach, describe, expect, it, vi } from "vitest";
import {
  APP_SIDE_HOME,
  APP_SIDE_LAST_PATH_KEY,
  ENGINE_SIDE_PATHS,
  loadLastPaths,
  otherSide,
  partitionNavBySide,
  persistLastPaths,
  sideForPath,
} from "./app-side";

describe("sideForPath", () => {
  it("routes the user work surfaces to the user side", () => {
    for (const p of ["/sessions", "/chat", "/files", "/docs"]) {
      expect(sideForPath(p)).toBe("user");
    }
  });

  it("routes the meta/machinery surfaces to the engine side", () => {
    for (const p of ["/engine-room", "/regular", "/pacing", "/logs", "/system"]) {
      expect(sideForPath(p)).toBe("engine");
    }
  });

  it("treats unknown/plugin routes as user side (keeps user side full)", () => {
    expect(sideForPath("/kanban")).toBe("user");
    expect(sideForPath("/some-plugin")).toBe("user");
    expect(sideForPath("/")).toBe("user");
  });

  it("inherits the parent side for nested routes", () => {
    expect(sideForPath("/profiles/new")).toBe("engine");
    expect(sideForPath("/files/sub/dir")).toBe("user");
  });

  it("does not match a mere prefix that is not a path boundary", () => {
    // "/envelope" must NOT match the "/env" engine path.
    expect(sideForPath("/envelope")).toBe("user");
  });
});

describe("partitionNavBySide", () => {
  const items = [
    { path: "/sessions" },
    { path: "/regular" },
    { path: "/files" },
    { path: "/engine-room" },
    { path: "/kanban" },
  ];

  it("keeps only user items, in order", () => {
    expect(partitionNavBySide(items, "user")).toEqual([
      { path: "/sessions" },
      { path: "/files" },
      { path: "/kanban" },
    ]);
  });

  it("keeps only engine items, in order", () => {
    expect(partitionNavBySide(items, "engine")).toEqual([
      { path: "/regular" },
      { path: "/engine-room" },
    ]);
  });
});

describe("otherSide", () => {
  it("flips the two positions", () => {
    expect(otherSide("user")).toBe("engine");
    expect(otherSide("engine")).toBe("user");
  });
});

describe("last-path persistence", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function memoryStorage(): Storage {
    const map = new Map<string, string>();
    return {
      getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
      setItem: (k: string, v: string) => void map.set(k, v),
      removeItem: (k: string) => void map.delete(k),
      clear: () => map.clear(),
      key: (i: number) => Array.from(map.keys())[i] ?? null,
      get length() {
        return map.size;
      },
    } as Storage;
  }

  it("defaults to each side's home when nothing is stored", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    expect(loadLastPaths()).toEqual({
      user: APP_SIDE_HOME.user,
      engine: APP_SIDE_HOME.engine,
    });
  });

  it("round-trips persisted routes", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    persistLastPaths({ user: "/files", engine: "/logs" });
    expect(loadLastPaths()).toEqual({ user: "/files", engine: "/logs" });
  });

  it("falls back per-key on corrupt or partial storage", () => {
    const store = memoryStorage();
    store.setItem(APP_SIDE_LAST_PATH_KEY, JSON.stringify({ user: "/files" }));
    vi.stubGlobal("localStorage", store);
    expect(loadLastPaths()).toEqual({
      user: "/files",
      engine: APP_SIDE_HOME.engine,
    });

    store.setItem(APP_SIDE_LAST_PATH_KEY, "{not json");
    expect(loadLastPaths()).toEqual({
      user: APP_SIDE_HOME.user,
      engine: APP_SIDE_HOME.engine,
    });
  });

  it("tolerates storage throwing (private browsing)", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage);
    expect(() => persistLastPaths({ user: "/x", engine: "/y" })).not.toThrow();
    expect(loadLastPaths()).toEqual({
      user: APP_SIDE_HOME.user,
      engine: APP_SIDE_HOME.engine,
    });
  });
});

describe("ENGINE_SIDE_PATHS", () => {
  it("has no duplicates", () => {
    expect(new Set(ENGINE_SIDE_PATHS).size).toBe(ENGINE_SIDE_PATHS.length);
  });
});
