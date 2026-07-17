# ACP session metadata + interactive steering

Task `t_81c4183c`. Two deliverables for external ACP executor sessions
(`claude-code` / `codex`, driven by `agent/copilot_acp_client.py`):

1. **Interactive steering** — inject an operator message into a *live*
   turn-based session instead of only cancelling it.
2. **Full session metadata** — surface everything the adapter advertises per
   session (model, reasoning effort, permission mode, context-window state,
   cost) into `zeus.db` and the card's "last run" block.

## Per-session field inventory (with evidence)

Evidence sources, verified this task:

- ACP JSON schema shipped with the SDK:
  `@agentclientprotocol/sdk/schema/schema.json` (installed under the npx cache).
- The Claude Code adapter build code:
  `@agentclientprotocol/claude-agent-acp/dist/acp-agent.js`.
- ACP "Session Usage / Context Status" RFD:
  <https://agentclientprotocol.com/rfds/session-usage>.

### `session/new` → `NewSessionResponse`

| Field | Type | Notes |
|---|---|---|
| `sessionId` | string | required |
| `modes` | `{currentModeId, availableModes[]}` \| null | permission mode; adapter ids: `default`, `acceptEdits`, `bypassPermissions`, `plan`, `auto`, `dontAsk` (evidence: `applySessionMode` switch) |
| `configOptions` | `SessionConfigOption[]` \| null | per-session selectors, see below |
| `_meta` | object \| null | reserved extensibility |

`configOptions` ids emitted by claude-agent-acp (evidence: `*_CONFIG_ID`
constants + `buildConfigOptions`):

| id | meaning | currentValue |
|---|---|---|
| `mode` | permission mode (mirrors `modes`) | mode id |
| `model` | model | model id (e.g. `claude-opus-4-8`) |
| `effort` | reasoning effort — **only when the model supports it** | `default` or a level |
| `fast` | fast-mode toggle — only when supported | select/boolean |
| `agent` | custom sub-agent — only when configured | agent id |

Each `SessionConfigSelect` carries `{currentValue, options[{value,name,description}]}`.

### `session/update` notifications (streamed during a turn)

- `usage_update` → `UsageUpdate`: `used` (int, req), `size` (int, req, context
  window), `cost` (`{amount:double, currency}`, optional). claude-agent-acp
  populates all three: `size = contextWindowSize`, `cost.amount =
  total_cost_usd` (evidence: `acp-agent.js` usage_update emit). Client-derived:
  `remaining = size - used`.
- `current_mode_update` → `{currentModeId}`.
- `config_option_update` → `{configOptions}`.
- `agent_message_chunk` / `agent_thought_chunk` → response / reasoning text
  (already consumed).

### `session/prompt` → `PromptResponse`

- `stopReason` (required).
- `usage` (**UNSTABLE**) → `Usage`: `totalTokens`, `inputTokens`,
  `outputTokens` (req); `thoughtTokens`, `cachedReadTokens`,
  `cachedWriteTokens` (optional). Session-cumulative, not per-turn.

### Captured before / after this task

| Field | Before | Now |
|---|---|---|
| model | yes | yes |
| turn token usage | yes | yes |
| **reasoning effort** | no | yes → `zeus.db` + run metadata |
| **permission mode** | no | yes → run metadata |
| **context used/size/remaining** | `used` only, unused | yes → `zeus.db` + run metadata |
| **cost (USD)** | no | yes → `zeus.db` + run metadata |

## Wiring

- `copilot_acp_client.py`: captures `last_mode`, `last_effort` at `session/new`
  and `last_context` (`context_used`/`context_size`/`context_remaining`/
  `cost_usd`/`cost_currency`) from `usage_update`.
- `zeus.db` `token_usage`: new columns `effort`, `context_used`,
  `context_size`, `cost_usd` (fed by the `post_api_request` hook).
- Kanban `task_runs.metadata`: the executor stamps mode/effort/context/cost, so
  the Zeus card "last run" block renders `eff:high · ctx 45k/200k · $0.25`.

## Interactive steering (minimal path)

ACP sessions are turn-based; you cannot interrupt an in-flight turn, only queue
the next one. Design:

- **Control channel**: a per-task JSONL inbox at
  `<board_dir>/steer/<task_id>.jsonl`. The Zeus dashboard `POST
  /api/plugins/zeus/steer/{task_id}` appends `{"text": ...}` (card context-menu
  → "Направить сообщение сессии").
- **Executor**: `CopilotACPClient._run_prompt(..., follow_up=cb)` keeps the
  session open after the primary turn and replays each queued message as another
  `session/prompt` on the *same* session until the inbox drains. Consumed lines
  are moved to a `.done.jsonl` sibling so a steer is never lost.
- Toggle: `HERMES_ACP_STEERING=0` disables (default on; empty inbox is a
  one-stat no-op).

### Known limitation

A steer message queued exactly while a pooled Claude session is being rotated
off on a usage limit may be consumed on the losing session (its turn re-runs on
the fresh subscription without it). The message is preserved in `.done.jsonl`
for manual replay. Acceptable for a rarely-used path.
