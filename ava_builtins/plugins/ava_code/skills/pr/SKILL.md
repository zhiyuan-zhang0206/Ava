---
name: pr
description: Checks commits, pushes, PR creation, dependencies, trailers, and repository hygiene. Use before committing, pushing, or opening any PR, even when another workflow already covers part of the release path.
---

# Ava Code — committing, pushing & PRs

## Push self-check

Before opening a PR, go through this item by item:

1. **Rebase**: rebased onto the latest `main`, no stale base.
2. **Conflict**: all conflicts resolved, no `<<<<<<<` markers left.
3. **Commit message**: follows convention (includes `Co-authored-by: Ava #<id>`), description accurate.
4. **Local checks green**: run the check set for the scope you touched (see the `testing` skill) and confirm it passes — pushing to let CI find failures wastes a round-trip.

**Goal: the PR is green on CI on the first try 99% of the time.** Run checks locally and only push when green — don't rely on GitHub CI to debug for you.

## PR creation confirmation

The `https://github.com/.../pull/new/...` link GitHub returns is **just a page that lets you click to create a PR** — it is NOT an already-created PR. You must actually create it:

```bash
gh pr create --base main --title "[Ava-{id}] ..." --body "..."
```

Then verify immediately:

```bash
gh pr list --head $(git branch --show-current) --json number,url
```

If this returns empty, the PR was not created — don't pretend a PR exists; investigate right away.

PR title and description are in **Chinese**.

## Dependency declaration

If your PR depends on another not-yet-merged PR (e.g. you need its schema or API change), declare it at the top of the PR description:

```
> Depends on: #XXX — can only rebase and pass CI once that is merged.
```

This stops the reviewer/merger from blaming you when a downstream PR's CI fails purely because the dependency isn't merged yet.

## Commit trailer mechanics

The trailer format (`Co-authored-by: Ava #<agent_id>`) is in your coding-tools section. The mechanics: with `git commit -m`, splice the trailer into the message manually; with `-F <file>`, ensure the file ends with a blank line before the trailer.

**Why the Agent ID is required:** without the number, `Co-authored-by: Ava` cannot be distinguished among hundreds of Ava instances; `#<id>` makes `git log` traceable to a specific instance.

## Repo hygiene

Before committing, don't drag scratch into the repo — git history is permanent. Put PR-body / commit-message drafts under `/tmp/`, never inside the repo. Confirm each path with `git status` before `git add` — don't `git add -A` / `git add .` blindly.

Repo-specific hygiene and documentation rules — which directories to keep tidy, how docs are expected to track code — live in that repo's `AGENTS.md`. Read it (see the `ava-code.conventions` skill / "AGENTS.md auto-injection") and follow it; don't assume one repo's conventions apply to another.
