---
type: doc
title: Source Tree Guard
description: Prod source checkout integrity — periodic reset to the installed commit, tamper detection in the health probe, and the runtime-artifact whitelist.
tags:
- cli
- lifecycle
- health
---

# Source Tree Guard

## What it is

The prod source checkout (`$AVA_HOME/source`, default `~/.ava/source`) is the
tree every agent process imports its code from, so a direct edit outside the
update flow can break `import ava` for the whole box within minutes — the
2026-08-28 outage class (a half-installed plugin left the tree in a state that
crashed every agent tool call; earlier incidents shared the same shape). User
ruling 2026-08-28: the tree must be kept whole — periodically reset to the
installed commit, tampering detected before exec crashes, and legitimate
runtime artifacts allowlisted. `shared/source_tree_guard.py` owns the
whitelist and the git reads; two callers use it, one whitelist, so the
detector and the fixer can never disagree about what is legal.

## Lifecycle flow

- **Detection** — `ava cluster health-probe` check 8 (OS-cron cadence, every 5
  minutes) calls `source_tree_violations`: tracked files changed vs HEAD,
  untracked files outside the whitelist, and HEAD moved off the last fully
  installed commit (`installed_sha`, the same bookmark the start-time
  source-integrity guard compares). Alert-only, same placement as checks 5-7:
  it fails the probe and wakes the owner, but never feeds the auto-rollback
  counter — rollback does not undo an on-disk edit. The probe never writes.
  A checkout the guard cannot evaluate (not a git checkout, or a git command
  failed) reports a distinct **"guard skipped"** alert instead of a clean
  pass: a broken git is exactly the state in which tampering becomes
  invisible, so the guard names itself as failing rather than looking clean.
- **Repair** — `_converge` registers the "source tree reset + clean"
  host-global step as the first converge step: `git reset --hard` to
  `installed_sha` and `git clean -fd` of untracked files outside the
  whitelist, at every `ava start` / `ava cluster update`. `ava start` also
  runs the same reset immediately BEFORE its source-integrity guard, so the
  guard never adopts drift as the installed commit on the regular
  operational path. The reset runs when HEAD moved off the installed commit
  OR any tracked file is dirty — the incident's main shape (a tracked file
  edited in place, never committed) leaves HEAD == installed, and
  `git reset --hard` to the same commit still discards working-tree
  modifications. Repairs print a warning and emit the registered anomaly
  event `source_tree_reset` with the reset range, cleaned paths, and kept
  whitelisted paths.
- **Deploy exemption** — the step is skipped while a cluster update is in
  flight: a rollout legitimately owns the tree (checkout happens before
  `installed_sha` is recorded), so the guard never fights a live update.
  `uv sync` and the frontend build never trip the guard: they write only
  gitignored paths or whitelisted ones.
- `converge_host` rejects host-global steps in `.worktrees/` and
  `.claude/worktrees/` checkouts, so a dev worktree — a development context by
  construction, always dirty — is never reset against the prod bookmark.

## Whitelist inventory

The whitelist is the inventory of non-ignored runtime artifacts legitimately
produced inside the checkout. As of 2026-08-28:

- `frontend/` — the built UI bundle the gateway serves (the frontend
  session's `npm run build` output: `.next/`, `tsconfig.tsbuildinfo`,
  `next-env.d.ts`; `node_modules` is already gitignored).

Gitignored paths (`.venv/`, `__pycache__/`, `.env`, …) are invisible to both
the detector and `git clean -fd`, so they need no whitelist entry.

## Invariants

- The reset target is `installed_sha` (the last fully installed commit), not
  the cluster pin: the pin is cluster-wide and legitimately ahead of a host
  that missed a rollout, and resetting to it would be an undeployed update.
- A missing `installed_sha` disables the HEAD reset (nothing to judge
  against) but not the untracked cleanup.
- A git failure during repair is reported (printed + left for the probe to
  keep alerting on), never raised — repair must not block a start.
- The whitelist validates itself at import: non-empty and free of
  catch-all patterns (anything that would whitelist arbitrary paths). An
  empty or catch-all whitelist silently disables detection on one side or
  false-alarms on the other, so it raises instead of running. Tests lock the
  shipped entries against the same misconfigurations.
- Detection is never silently blind: `() `means the guard saw a clean tree;
  a checkout it could not evaluate carries a `guard skipped:` marker.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[editable-install-guard.ava.okf.md]] — the sibling guard for the prod
  venv's editable-install pointer (same probe-detects / converge-repairs shape)
- [[cli/cli.ava.okf.md]] — public converge, start, and update surfaces
