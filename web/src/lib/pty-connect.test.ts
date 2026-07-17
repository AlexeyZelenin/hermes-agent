import { describe, expect, it } from "vitest";

import { buildPtyConnectParams, isZellijAttach } from "./pty-connect";

describe("isZellijAttach", () => {
  it("is true for a real session name", () => {
    expect(isZellijAttach("operator")).toBe(true);
  });

  it("is false for null / empty / whitespace", () => {
    expect(isZellijAttach(null)).toBe(false);
    expect(isZellijAttach(undefined)).toBe(false);
    expect(isZellijAttach("")).toBe(false);
    expect(isZellijAttach("   ")).toBe(false);
  });
});

describe("buildPtyConnectParams — gateway chat mode", () => {
  it("always carries the channel", () => {
    const p = buildPtyConnectParams({ channel: "chat-1", attachToken: "tok" });
    expect(p.channel).toBe("chat-1");
  });

  it("includes attach, resume, fresh, and profile when set", () => {
    const p = buildPtyConnectParams({
      channel: "chat-1",
      attachToken: "tok",
      resume: "sess-42",
      profile: "work",
      forceFresh: true,
    });
    expect(p).toEqual({
      channel: "chat-1",
      resume: "sess-42",
      fresh: "1",
      attach: "tok",
      profile: "work",
    });
  });

  it("omits optional params that are falsy", () => {
    const p = buildPtyConnectParams({
      channel: "chat-1",
      attachToken: "tok",
      resume: null,
      profile: "",
      forceFresh: false,
    });
    expect(p).toEqual({ channel: "chat-1", attach: "tok" });
  });
});

describe("buildPtyConnectParams — live zellij attach mode", () => {
  it("sends only the trimmed zellij session and nothing else", () => {
    const p = buildPtyConnectParams({
      zellij: "  operator  ",
      channel: "chat-1",
      attachToken: "tok",
      resume: "sess-42",
      profile: "work",
      forceFresh: true,
    });
    // Gateway params are dropped — zellij owns session persistence and the
    // backend routes ?zellij= before channel/keep-alive/profile resolution.
    expect(p).toEqual({ zellij: "operator" });
  });

  it("falls back to gateway mode when zellij is blank", () => {
    const p = buildPtyConnectParams({
      zellij: "   ",
      channel: "chat-1",
      attachToken: "tok",
    });
    expect(p).toEqual({ channel: "chat-1", attach: "tok" });
  });
});
