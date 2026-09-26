---
type: doc
title: Source Tree Guard
description: Tamper detection for a home that executes its source checkout — health-probe check 8 and the runtime-artifact whitelist.
tags:
- cli
- lifecycle
- health
---

# Source Tree Guard

## What it is

A source-run home imports its code from the prod source checkout
(`$AVA_HOME/source`, default `~/.ava/source`), so an in-place edit can break
`import ava` for the whole box within minutes — the 2026-08-28 outage class (a
half-installed plugin left the tree in a state that crashed every agent tool
call; earlier incidents shared the same shape). User ruling 2026-08-28:
tampering is detected before exec crashes, and legitimate runtime artifacts are
allowlisted. `shared/source_tree_guard.py` owns the whitelist and the git reads.

## Lifecycle flow

- **Detection** — `ava cluster health-probe` check 8 (OS-cron cadence, every 5
  minutes) calls `source_tree_violations`: tracked files changed vs HEAD and
  untracked files outside the whitelist. Alert-only, same placement as checks
  5-7: it fails the probe and wakes the owner; rollback does not undo an
  on-disk edit. The probe never writes, and nothing repairs the tree for the
  operator.
- **Scope** — the check applies only while the home runs its source checkout.
  A home with a selected release image (`$AVA_HOME/releases/current-release`)
  executes verified image bytes, so a leftover checkout cannot reach running
  code and is not inspected. An unreadable selector leaves the executing code
  unknown and reports **"guard skipped"**, never a clean pass.
- **HEAD moves are not tamper** — no current lifecycle records an installed
  commit for a source checkout, and an operator legitimately moves the checkout
  before an `ava restart`. Running-versus-checkout drift is shown, not alerted,
  by the roster `code` column. A `$AVA_HOME/installed_sha` file left by the
  retired updater is inert.
- A checkout the guard cannot evaluate (not a git checkout, or `git status`
  failed) reports a distinct **"guard skipped"** alert instead of a clean
  pass: a broken git is exactly the state in which tampering becomes
  invisible, so the guard names itself as failing rather than looking clean.

## Whitelist inventory

The whitelist is the inventory of non-ignored runtime artifacts legitimately
produced inside the checkout. As of 2026-08-28:

- `frontend/` — the built UI bundle the gateway serves (the frontend
  session's `npm run build` output: `.next/`, `tsconfig.tsbuildinfo`,
  `next-env.d.ts`; `node_modules` is already gitignored).

Gitignored paths (`.venv/`, `__pycache__/`, `.env`, …) are invisible to the
detector, so they need no whitelist entry.

## Invariants

- The whitelist validates itself at import: non-empty and free of
  catch-all patterns (anything that would whitelist arbitrary paths). An
  empty or catch-all whitelist silently disables detection on one side or
  false-alarms on the other, so it raises instead of running. Tests lock the
  shipped entries against the same misconfigurations.
- Detection is never silently blind: `()` means the guard saw a clean tree;
  a checkout it could not evaluate carries a `guard skipped:` marker.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[editable-install-guard.ava.okf.md]] — the sibling guard for the prod
  venv's editable-install pointer
- [[cli/release_transition/release_transition.ava.okf.md]] — the release
  selector that decides whether a home runs an image
