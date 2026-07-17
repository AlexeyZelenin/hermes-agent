# Repository Branch Protection (PR-only `main`)

> **Audience:** Repo owner / operators of the development fork
> **Scope:** GitHub repository `AlexeyZelenin/hermes-agent` (the development fork)
> **Managed via:** GitHub **repository ruleset** (`PR-only main protection`, id `19118456`)

## Overview

The development fork's default branch (`main`) is protected by a GitHub
**ruleset** so that changes land through pull requests instead of direct
pushes. This matches how work already flows on the fork: task branches
(`zeus/t_*`) are pushed and merged into `main` via PRs, while `main` itself is
otherwise only fast-forwarded from upstream (`NousResearch/hermes-agent`).

Rulesets are used in preference to legacy branch-protection rules; they are the
current GitHub mechanism and expose the same guarantees (require PR, block
force-push, block deletion) with per-role bypass.

## What is enforced

Ruleset **`PR-only main protection`** (`enforcement: active`), targeting the
repository default branch (`~DEFAULT_BRANCH`, i.e. `main`):

| Rule | Effect |
| --- | --- |
| `pull_request` | `main` can only be updated through a merged PR — **direct `git push` to `main` is rejected**. `required_approving_review_count: 0` (no human review is forced, so automated/self-merge is not deadlocked). Allowed merge methods: merge, squash, rebase. |
| `non_fast_forward` | Force-pushes to `main` are rejected. |
| `deletion` | Deleting `main` is rejected. |

**Bypass:** the **Repository admin** role (`RepositoryRole` id `5`, bypass mode
`always`). This is deliberate:

- The upstream-sync path (`hermes update` → `_sync_fork_with_upstream`,
  `hermes_cli/main.py`) does `git push origin main --force-with-lease` to
  fast-forward the fork's `main` from upstream. That push is a **direct** push
  and would otherwise be blocked by the `pull_request` rule. The owner runs it
  as an admin, so the admin bypass keeps fork-sync working.
- Non-admin collaborators (read / triage / write without admin) get no bypass:
  for them `main` is strictly PR-only.

No required status checks are configured. Adding a required check that does not
run on the fork would deadlock merges; CI still runs on PRs and can be made
required later once the fork's check names are pinned.

## How to verify

Confirm the rules are live on `main` (non-mutating):

```bash
gh api repos/AlexeyZelenin/hermes-agent/rules/branches/main
# → lists active rules: deletion, non_fast_forward, pull_request (ruleset_id 19118456)

gh api repos/AlexeyZelenin/hermes-agent/rulesets/19118456
# → enforcement:"active", bypass_actors:[RepositoryRole 5 / always]
```

Confirm a direct push to `main` is blocked. Because the repo **admin** has an
`always` bypass, an admin's own push succeeds — so test from a context that is
**not** an admin (a collaborator with only write/triage, or a token without
admin). Such a push fails with:

```
remote: error: GH013: Repository rule violations found for refs/heads/main.
remote: - Cannot update this protected ref ... Changes must be made through a pull request.
```

To sanity-check the mechanism as admin without pushing, temporarily flip the
ruleset to `evaluate`/`disabled` is **not** needed — the branch-rules endpoint
above is the authoritative proof of what is enforced.

## Managing the ruleset

```bash
# Inspect
gh api repos/AlexeyZelenin/hermes-agent/rulesets/19118456

# Update (edit a JSON payload, then)
gh api --method PUT repos/AlexeyZelenin/hermes-agent/rulesets/19118456 --input ruleset.json

# Remove (full revert — restores unrestricted pushes to main)
gh api --method DELETE repos/AlexeyZelenin/hermes-agent/rulesets/19118456
```

### Tightening later (optional)

If the fork should be strictly PR-only even for admins (no upstream-sync escape
hatch), remove the `bypass_actors` entry. Fork-sync would then need to push a
branch and open a PR instead of force-pushing `main` directly.
