// Query-parameter builder for the `/api/pty` WebSocket the terminal chat
// panel connects to. Kept as a pure function so the two connection modes can
// be unit-tested without standing up an xterm/WebSocket in jsdom.
//
// Two modes, mutually exclusive:
//
//   1. Gateway chat (default): spawn/attach a `hermes --tui` PTY child behind
//      the keep-alive registry, tied to a gateway `channel` (sidebar events)
//      and optionally scoped to a management `profile` / resumed session.
//
//   2. Live zellij attach ("variant A", task t_d2259745): mirror an existing
//      named zellij session instead of spawning a fresh TUI. zellij owns its
//      own session persistence, so the gateway channel / keep-alive / profile
//      params do not apply — the backend routes `?zellij=` *before* any of
//      them (see web_server.pty_ws). Emitting them would be dead weight, so
//      this mode sends only `zellij`.
export interface PtyConnectOptions {
  /** Live zellij session name to attach to; enables mode 2 when non-empty. */
  zellij?: string | null;
  /** Gateway channel id tying this PTY child to its sidebar (mode 1). */
  channel: string;
  /** Resume an existing session id (mode 1). */
  resume?: string | null;
  /** Scope the PTY child to a management profile's HERMES_HOME (mode 1). */
  profile?: string | null;
  /** Force a brand-new PTY child, rotating the keep-alive token (mode 1). */
  forceFresh?: boolean;
  /** Keep-alive reattach token for this tab (mode 1). */
  attachToken?: string | null;
}

/** True when `zellij` selects the live-attach mode (non-empty after trim). */
export function isZellijAttach(zellij?: string | null): boolean {
  return !!zellij && zellij.trim().length > 0;
}

export function buildPtyConnectParams(
  opts: PtyConnectOptions,
): Record<string, string> {
  const zellij = opts.zellij?.trim();
  if (zellij) {
    return { zellij };
  }
  const params: Record<string, string> = { channel: opts.channel };
  if (opts.resume) params.resume = opts.resume;
  if (opts.forceFresh) params.fresh = "1";
  if (opts.attachToken) params.attach = opts.attachToken;
  if (opts.profile) params.profile = opts.profile;
  return params;
}
