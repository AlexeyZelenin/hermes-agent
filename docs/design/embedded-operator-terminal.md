# Embedded operator terminal — variant A (live zellij attach)

Task `t_d2259745`. The operator drives fleet work (spawning agents, watching
FleetView) inside a persistent **zellij** session from a native terminal
(Ghostty). To iterate on the dashboard UI while that work runs, the operator
wants the same session visible **inside** the Zeus dashboard — not a bridge,
not a reseeded copy.

Operator decision (2026-07-17): **variant A = embed the terminal**. A bridge or
a fresh reseed breaks the native Claude Code runtime the operator needs to
spawn and observe agents, so the browser must mirror the *live* session, not
start a new one.

## What ships in this task

The load-bearing capability: `/api/pty` can attach the browser terminal to a
**live named zellij session**, so the exact session rendered in Ghostty renders
in the dashboard too.

`web/` already bundles `@xterm/xterm` and paints `/api/pty` in
`src/pages/ChatPage.tsx`; the endpoint previously only ever spawned a fresh
`hermes --tui`. The change adds one branch.

### Backend seam

`hermes_cli/web_server.py`:

- `pty_ws` (`@app.websocket("/api/pty")`) recognises `?zellij=<session>` and
  routes to `_pty_attach_zellij` **before** the channel / keep-alive /
  chat-argv resolution — a raw zellij client has no gateway sidecar.
- `_pty_attach_zellij` validates the name, builds the argv, spawns it behind
  the existing `PtyBridge`, and hands the socket to `_legacy_pump`.
- `_zellij_attach_argv(session, create=)` → `["zellij", "attach", (--create),
  <session>]`. `zellij` is resolved via `shutil.which` (`_resolve_zellij_bin`).

Why `_legacy_pump` (1:1, kill-on-disconnect) and **not** the keep-alive
`PTY_REGISTRY`: zellij owns session persistence itself. `zellij attach` is a
thin **client** to the zellij **server** daemon that holds the panes,
statusline, and running agents. So on browser disconnect we *want* the attach
client torn down — the session (and its agents) survives in the server, ready
for the next attach. This is zellij's documented client/server model; it was
not exercised live here to avoid leaving a stray zellij server on the operator
machine.

### Security

Same gate as the rest of `/api/pty`, enforced in `pty_ws` before the branch:

- **Localhost-only.** `_ws_client_reason` rejects non-loopback peers; uvicorn
  binds `127.0.0.1`. A shell attached to the operator's session runs with the
  operator's `--skip-permissions` privileges — an open shell — so it must
  never be reachable off-loopback.
- **Session token** (`?token=`), same ephemeral `_SESSION_TOKEN` as REST.
- **Argv-injection proof.** `_ZELLIJ_SESSION_RE = ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
  keeps the name a single positional arg: no leading `-` (would read as a
  zellij flag), no whitespace or shell metacharacters. `PtyBridge.spawn` execs
  the argv directly (no shell), so there is no shell-interpolation surface even
  before the regex.

### Reachable today

With the dashboard running, open a browser terminal on the live session:

```
ws://127.0.0.1:<port>/api/pty?token=<session_token>&zellij=<session_name>
```

Drive it with the same xterm.js setup `ChatPage.tsx` uses (send
`\x1b[RESIZE:cols;rows]` on fit; forward keystrokes as bytes). `?create=1`
falls back to creating the session if it does not exist; the default is strict
attach, because variant A wants the operator's *existing* session.

## Deliberately deferred: the three-pane layout

The task also asks for terminal + kanban board + card visible **at once**, next
to each other. That is a product-design decision about the operator's dashboard
layout, not a mechanical wiring task, and the operator flagged the embedded
terminal as possibly-not-final ("прагматика: тайлить окно рядом с браузером =
90% пользы без кода"). Building a speculative multi-pane React layout that
can't be visually reviewed in this session would violate the no-speculative-UI
rule, so it is **not** built here. The backend seam above is what any such
layout needs; the pane itself should be designed with the operator's eyes on
it.
