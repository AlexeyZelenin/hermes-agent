# Zeus mechanical watchdog

A **sealed, LLM-free** liveness monitor for the Zeus autonomy stack. It lives
*outside* the thing it watches — a standalone `launchd` agent, not a gateway
cron — so it survives a dead or wedged gateway. It is pure mechanics: no LLM, no
network except one `curl` to the Telegram Bot API, and **stdlib-only** so it
keeps working even when every agent and the whole `hermes` package are broken.

## What it checks

Each pass (default every 120 s) evaluates these conditions against the live
`kanban.db` / `zeus.db` and the gateway process:

| key | fires when |
| --- | --- |
| `gateway_dead` | gateway pid file missing/empty or its pid is not alive |
| `dispatcher_stale` | gateway alive but `gateway.log` hasn't been written for `dispatcher_log_stale_sec` (600 s) |
| `ready_no_run` | READY (assigned) tasks > 0 but RUN = 0, sustained for `ready_no_run_sec` (600 s) |
| `resume_stuck` | a subscription hit a limit recently, its cooldown has elapsed, yet READY > 0 / RUN = 0 (`resume_grace_sec`, 300 s) — auto-resume didn't fire |
| `heartbeat_stale:<task>` | a `running` task's last heartbeat is older than `heartbeat_timeout_sec` (7200 s) |
| `db_corrupt:<db>` | `PRAGMA quick_check` on `kanban.db`/`zeus.db` returns anything but `ok` |

All thresholds live in the config file. **Heartbeat caveat:** ACP executors
(`claude-code`) update `last_heartbeat_at` only ~hourly, so a healthy long
session shows ~70 min gaps. `heartbeat_timeout_sec` must stay above the slowest
executor's cadence (default 2 h) or it will false-positive on normal work.

## Alerting

A short Telegram message via **direct `curl`** to the Bot API:

- problem → `⚠️ Zeus нужно внимание: <what>`
- recovery → `✅ Zeus ожил: <what>` (one-shot, when a fired problem clears)

**Debounce:** at most one alert per problem per `debounce_sec` (30 min).

- Chat id: `~/.hermes/zeus/telegram_chat_id`.
- Bot token: `TELEGRAM_BOT_TOKEN` line in `~/.hermes/.env` (or set `bot_token`
  inline in the config). The token is resolved lazily and never logged.

## Install

```sh
zeus_watchdog/install.sh install     # template plist → LaunchAgents, load, dry-run
zeus_watchdog/install.sh status      # launchctl state + log tail
zeus_watchdog/install.sh uninstall
```

Config: copy `watchdog.config.example.json` to
`~/.hermes/zeus/watchdog.config.json` and edit. Every key is optional.

## Run manually

```sh
python -m zeus_watchdog --dry-run    # evaluate + print, send nothing, no state write
python -m zeus_watchdog              # one real pass
```

## Layout

- `config.py` — defaults + JSON overlay + token/chat-id resolution
- `checks.py` — best-effort IO probes + pure `evaluate`
- `state.py` — pure debounce/sustain/recovery `reconcile`
- `alert.py` — Telegram send (`curl`, injectable)
- `runner.py` — one-pass `run_once`
- `__main__.py` — CLI

`checks.evaluate` and `state.reconcile` are pure and unit-tested without any
live process or database.
