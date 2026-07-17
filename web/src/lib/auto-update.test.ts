import { describe, expect, it } from "vitest";
import {
  AUTO_UPDATE_DEFAULT,
  AUTO_UPDATE_STORAGE_KEY,
  readAutoUpdate,
  shouldRefreshVisible,
  visibleListSignature,
  visibleSignature,
  writeAutoUpdate,
} from "./auto-update";

// The lib runs in the `node` vitest env (no DOM), so stub a minimal
// in-memory localStorage the way the other lib tests stub globals.
function fakeStorage(seed?: Record<string, string>): Storage {
  const store = new Map<string, string>(Object.entries(seed ?? {}));
  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    removeItem: (k: string) => store.delete(k),
    setItem: (k: string, v: string) => void store.set(k, v),
  };
}

describe("readAutoUpdate", () => {
  it("defaults to ON when unset, when storage is missing, or on error", () => {
    expect(AUTO_UPDATE_DEFAULT).toBe(true);
    expect(readAutoUpdate(fakeStorage())).toBe(true);
    expect(readAutoUpdate(null)).toBe(true);
    expect(readAutoUpdate(undefined)).toBe(true);
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readAutoUpdate(throwing)).toBe(true);
  });

  it("reads the persisted preference", () => {
    expect(readAutoUpdate(fakeStorage({ [AUTO_UPDATE_STORAGE_KEY]: "0" }))).toBe(false);
    expect(readAutoUpdate(fakeStorage({ [AUTO_UPDATE_STORAGE_KEY]: "false" }))).toBe(false);
    expect(readAutoUpdate(fakeStorage({ [AUTO_UPDATE_STORAGE_KEY]: "1" }))).toBe(true);
  });

  it("round-trips through writeAutoUpdate", () => {
    const s = fakeStorage();
    writeAutoUpdate(s, false);
    expect(readAutoUpdate(s)).toBe(false);
    writeAutoUpdate(s, true);
    expect(readAutoUpdate(s)).toBe(true);
  });
});

describe("visible-field diff", () => {
  const fields = ["id", "title", "count"] as const;
  type Row = { id: string; title: string; count: number; hidden: number };
  const row = (over: Partial<Row> = {}): Row => ({
    id: "a",
    title: "T",
    count: 1,
    hidden: 0,
    ...over,
  });

  it("signature ignores fields outside the visible set", () => {
    expect(visibleSignature(row(), fields)).toBe(
      visibleSignature(row({ hidden: 999 }), fields),
    );
  });

  it("signature changes when a visible field changes", () => {
    expect(visibleSignature(row(), fields)).not.toBe(
      visibleSignature(row({ title: "X" }), fields),
    );
  });

  it("list signature is order-sensitive and honours the limit", () => {
    const a = row({ id: "a" });
    const b = row({ id: "b" });
    expect(visibleListSignature([a, b], fields)).not.toBe(
      visibleListSignature([b, a], fields),
    );
    // Beyond the limit, an invisible-to-the-user tail change is not seen.
    expect(visibleListSignature([a, b], fields, 1)).toBe(
      visibleListSignature([a, row({ id: "b", title: "different" })], fields, 1),
    );
  });

  it("an invisible-only churn produces an identical list signature", () => {
    const before = [row({ id: "a", hidden: 1 }), row({ id: "b", hidden: 2 })];
    const after = [row({ id: "a", hidden: 42 }), row({ id: "b", hidden: 99 })];
    expect(visibleListSignature(before, fields)).toBe(
      visibleListSignature(after, fields),
    );
  });
});

describe("shouldRefreshVisible", () => {
  it("never refreshes on the first poll (no baseline)", () => {
    expect(shouldRefreshVisible(null, "sig")).toBe(false);
  });

  it("refreshes only when the visible signature actually changed", () => {
    expect(shouldRefreshVisible("sig", "sig")).toBe(false);
    expect(shouldRefreshVisible("sig", "other")).toBe(true);
  });
});
