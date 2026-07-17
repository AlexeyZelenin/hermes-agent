# Protected-Path Gate — End-to-End Verification (t_2dec957d)

> **Scope:** development fork `AlexeyZelenin/hermes-agent`
> **Verified:** 2026-07-17, via GitHub REST API + local git (non-mutating checks only)
> **Companion docs:** [`repo-branch-protection.md`](./repo-branch-protection.md) (ruleset),
> `.github/CODEOWNERS` (owner rules)

This is a point-in-time verification of the GitHub-side protected-path gate
built by tasks `t_b146d27c` (PR-only ruleset) and `t_537e2195` (CODEOWNERS),
plus its relationship to the in-agent `propose_decision` tool-gate.

## Summary

| Requirement | Status | Evidence |
| --- | --- | --- |
| Direct push to `main` rejected | ✅ **live** | ruleset `19118456` active; `pull_request` rule enforced on `main` |
| Force-push / delete `main` rejected | ✅ **live** | `non_fast_forward` + `deletion` rules enforced |
| Normal PR follows PR policy (auto-merge, no forced human review) | ✅ **live** | `required_approving_review_count: 0`; PR #1 `mergeable`, `reviewDecision: ""` |
| PR touching `migrations/**` or `openapi*` requires code-owner review | ❌ **NOT enforced** | two independent gaps, see below |
| Complements (not conflicts with) `propose_decision` | ✅ | different layers, no shared enforcement point |

## What is live and working

Ruleset **`PR-only main protection`** (`id 19118456`, `enforcement: active`) on
the fork's default branch. Confirmed via
`gh api repos/AlexeyZelenin/hermes-agent/rules/branches/main`:

- `pull_request` — direct `git push` to `main` is rejected; changes must land via PR.
- `non_fast_forward` — force-push to `main` rejected.
- `deletion` — deleting `main` rejected.
- `required_approving_review_count: 0` — a normal PR is mergeable without human
  review, so the autonomous swarm's PRs are not deadlocked. Verified on live PR
  #1: `mergeable: MERGEABLE`, `reviewDecision: ""`, `reviewRequests: []`.
- **Bypass:** `RepositoryRole` id `5` (Repository admin), `always` — keeps the
  `hermes update` upstream-sync force-push working. **Caveat:** because the
  admin bypasses, a *direct-push-is-blocked* test must be run from a non-admin
  token to observe the rejection; the admin's own push succeeds. The
  branch-rules API above is the authoritative proof of enforcement.

## The gap — the migrations/openapi code-owner gate is NOT in effect

The task requires that a PR changing `migrations/**` or `openapi*` require a
designated code-owner review. **Today it does not** — for two independent
reasons, either of which alone defeats the gate:

1. **`require_code_owner_review: false`** in the ruleset's `pull_request` rule.
   Even if CODEOWNERS matched a changed file, GitHub would only *request* the
   owner as a reviewer, not *require* their approval to merge.

2. **`.github/CODEOWNERS` is not on `main`.** GitHub reads CODEOWNERS from the
   PR's base branch (`main`) / default branch. The file (commit `eaefd9b`)
   currently lives **only on the local `zeus/*` working branch and is not merged
   to `fork/main`** (verified: `git merge-base --is-ancestor eaefd9b fork/main`
   → not an ancestor; `git ls-tree fork/main .github/CODEOWNERS` → absent). Until
   it lands on `main`, **no code owner is even requested** on any PR.

**Consequence:** a PR that adds `migrations/0001_init.sql` or edits
`openapi.yaml` merges under the plain 0-approval PR policy — the high-risk-path
review is silent-no-op.

### Remediation (two steps, in order)

1. **Merge CODEOWNERS to `main`** (open/merge a PR carrying `.github/CODEOWNERS`,
   or fast-forward it in). Owner rules only exist for GitHub once the file is on
   the base branch.
2. **Enable code-owner review** on the ruleset:
   set `require_code_owner_review: true` in the `pull_request` rule via
   `gh api --method PUT repos/AlexeyZelenin/hermes-agent/rulesets/19118456 --input ruleset.json`.

### Known limitation that blocks step 2 — sole-owner self-approval deadlock

`@AlexeyZelenin` is the **only** account with write access, and the same account
authors the `zeus/*` PRs. GitHub does not allow approving your own PR, so once
`require_code_owner_review` is on, an owner-authored protected-path PR **cannot
be satisfied by review** — it can only proceed via the **admin `always`
bypass** (a manual admin merge, not auto-merge). Net effect once both steps are
done: protected-path PRs stop auto-merging and require an explicit human admin
action — which is arguably the *intended* "escalate to a human" behavior, but it
is a behavior change, not a no-op. A clean fix is to add a second maintainer or
a `@org/team` owner so the gate is satisfiable by review without bypass.

This is why the parent task deliberately left `require_code_owner_review: false`.
Enabling it is a **product/ops decision** (changes merge behavior for the sole
owner) and was therefore reported here rather than flipped unilaterally.

## Relationship to `propose_decision` — complements, does not conflict

`propose_decision` is an **in-agent protocol tool-gate**, distinct from the
GitHub gate. Its only footprint in this repo is a reserved name in
`agent/budget_guard.py`'s `EXEMPT_PROTOCOL_TOOLS` (alongside the implemented
`kanban_block` / `kanban_comment`): it is always allowed even past a budget
hard-stop, so a throttled worker can still escalate a decision to the operator.

> **Honest gap:** `propose_decision` is not yet an implemented, registered tool
> (no schema, no handler) — only the lifeline reservation exists. `kanban_block`
> and `kanban_comment` are the real, wired escalation tools today.

The two gates sit at different layers and share no enforcement point, so they
cannot conflict:

| | `propose_decision` (in-agent) | protected-path gate (GitHub) |
| --- | --- | --- |
| **Nature** | cooperative — the worker *voluntarily* surfaces a decision | involuntary — enforced by GitHub regardless of agent behavior |
| **When** | *before* acting, at the worker's discretion | *at merge/push time*, unconditionally |
| **Failure mode it covers** | worker wants human sign-off and asks for it | worker skips escalation / misbehaves — the merge is still blocked |

They are **defense in depth**: `propose_decision` is where a well-behaved worker
escalates; the ruleset + CODEOWNERS is the hard backstop that catches a
protected-path change or a direct push to `main` even if the worker never calls
it. The one seam worth noting: **nothing currently forces** a worker to call
`propose_decision` before opening a protected-path PR — that coupling is a
protocol convention, not machinery. The GitHub gate (once fully enabled) is what
turns that convention into an enforced stop.

## How to re-verify (non-mutating)

```bash
# Rules live on main
gh api repos/AlexeyZelenin/hermes-agent/rules/branches/main
#   → deletion, non_fast_forward, pull_request (ruleset_id 19118456)

# Full ruleset incl. the code-owner-review flag
gh api repos/AlexeyZelenin/hermes-agent/rulesets/19118456
#   → pull_request.parameters.require_code_owner_review  (currently false)

# CODEOWNERS presence on the base branch
git ls-tree fork/main .github/CODEOWNERS        # currently absent

# Normal-PR policy on a live PR
gh pr view 1 --repo AlexeyZelenin/hermes-agent --json mergeable,reviewDecision,reviewRequests
#   → mergeable, no forced review, no code owner requested
```
