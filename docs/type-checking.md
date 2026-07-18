# Python type-checking (strict gate)

The Python codebase (`agent/`, `hermes_cli/`, `plugins/`) is guarded by a
**strict pyright type-check gate** in CI. It gives most of the safety a
statically-typed language would, without leaving Python.

## What runs where

| Tool | Config | Mode | Blocks merge? |
| --- | --- | --- | --- |
| **pyright** | `pyrightconfig.json` | strict correctness gate | **yes** — `pyright-gate` job in `.github/workflows/lint.yml` |
| ty | `[tool.ty.*]` in `pyproject.toml` | advisory diff vs base branch | no (PR comment only) |

The two are complementary: `ty` posts an informational diff of new
diagnostics on every PR; pyright is the hard gate that fails the build. Keep
both.

## Why pyright, and what "strict" means here

pyright is the type checker built by the TypeScript team, so it gives the
closest thing to TS-grade safety in Python. Its `typeCheckingMode: "strict"`
is the baseline in `pyrightconfig.json`.

Literal, unmodified strict is impractical to *adopt* on an existing large
codebase: the `reportUnknown*` / `reportMissing*Type` rule family fires on
every value that flows in from an untyped import, and until the *entire*
transitive import graph is typed those errors are unactionable noise that
buries real bugs (e.g. `hermes_cli/kanban_db.py` reported 567 strict errors,
~90% of them this cascade). So the config keeps **every high-value soundness
rule as a hard error** — optional-member access, argument/return/assignment
type mismatches, call/index issues, possibly-unbound, unreachable/redundant
comparisons, unused variables, operator misuse, missing imports — and relaxes
only that untyped-import-noise family. See the inline comments in
`pyrightconfig.json` for the exact list and rationale.

## The gate is scoped by `include`

`pyrightconfig.json`'s `include` list **is** the gated zone. Bare `pyright`
checks exactly those paths and CI fails on any error. Import resolution always
uses the repo root, so files outside `include` are still read for types — they
just don't produce gating diagnostics.

Currently gated (the new/critical modules):

- `agent/acp_task_executor.py` — the ACP task executor
- `agent/claude_subscriptions.py`, `agent/claude_subscription_aux.py` — subscriptions
- `hermes_cli/zeus_pacing.py` — pacing
- `hermes_cli/kanban_db.py` — kanban DB

## Run it locally

```bash
# one-time: install the pinned checker
uv tool install pyright@1.1.411

# ensure deps are present so imports resolve (pyright reads .venv)
uv sync --extra all --extra dev

# run the gate exactly as CI does
pyright
```

`pyright <path>` also works for a single file (uses the same config).

## Expanding the strict zone

This is meant to grow. Two independent axes:

### 1. More modules (widen coverage)

Add paths to `include` in `pyrightconfig.json`, fix what surfaces, commit.
Natural next candidates: `agent/tool_executor.py`, `hermes_cli/subscription_limits.py`,
`hermes_cli/nous_subscription.py`. The end state is:

```json
"include": ["agent", "hermes_cli", "plugins"]
```

Work module-by-module so each PR stays reviewable and the gate never regresses.

### 2. Tightening toward full strict (deeper checks)

As a module's dependencies gain type annotations, the relaxed `reportUnknown*`
noise for it drops. Re-enable those rules — globally by flipping the value in
`pyrightconfig.json` back to `"error"`, or per-file with a top-of-file
`# pyright: strict` comment / `# pyright: reportUnknownMemberType=error`
pragma. Prefer per-file pragmas while the global graph is still untyped, so one
fully-typed module can run at true strict without forcing the rest to.

## Suppressing a single line

Use a scoped, justified ignore — never a blanket `# type: ignore`:

```python
if not isinstance(child, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
```

Always name the rule and leave a comment explaining why the checker is wrong
(usually: runtime input that can violate the annotation).
