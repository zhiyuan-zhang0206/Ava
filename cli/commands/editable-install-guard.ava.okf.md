---
type: doc
title: Editable Install Guard
description: Prod lifecycle assertion and update-time permission window for Ava's editable-install pointer.
tags:
- cli
- lifecycle
- update
---

# Editable Install Guard

## What it is

An Ava editable install is a path pointer stored in
`<checkout>/.venv/.../site-packages/_editable_impl_ava.pth`, plus the editable
source URL uv records in the matching `<dist-info>/direct_url.json`. Both must
name a stable source root; a disposable worktree is never a legal target.
`shared/editable_install.py` owns the platform-independent discovery, exact-root
validation, atomic repair, and temporary permission window used by lifecycle
callers.

## Lifecycle flow

- `_converge` registers the prod assertion as a host-global step. It resolves
  the installed prod checkout, accepts that source plus explicitly allowlisted
  stable dev-clone roots, and repairs every other exact target to prod source.
  The same pass asserts the `direct_url.json` records: a URL naming anything
  outside the allowlist (or a record that is unparsable or not marked
  editable) is repaired too, so the pointer and the URL can never disagree.
  Repairs print a warning and emit the registered anomaly events
  `editable_pth_repaired` / `editable_direct_url_repaired` with the path,
  poisoned target, and source root.
- `converge_host` rejects host-global steps before execution in `.worktrees/`
  and `.claude/worktrees/` checkouts, so a worktree's own legal pointer is never
  inspected or rewritten. `ava start` inherits the same converge step.
- Update, recovery, rollback, and start's source-integrity auto-heal route each
  `uv sync` through `editable_pth_write_window`. The window temporarily opens
  both the editable records and their site-packages directories, so uv's atomic
  replacement can complete; their exact original modes are restored in
  `finally`, including non-zero syncs. It discovers all existing site-packages
  directories in the supported virtualenv layouts structurally, so a partially
  failed sync cannot hide a protected directory by deleting its records. Outside
  an active cluster update, converge protects those POSIX directories as `0555`,
  which rejects an unsanctioned replacement at the directory boundary. During an
  update it defers that protection until the next quiescent start, allowing an
  already-running orchestrator and recovery path to complete their syncs.
- The native Windows deployment chain calls
  `python -m cli.commands._update_uv_sync`, giving it the same write window as
  the in-process POSIX/WSL paths. Discovery scans Windows `Lib`, POSIX `lib`,
  and `lib64` virtualenv layouts explicitly.

## Invariants

- Allowlisting is exact-root based. An allowlisted dev clone does not permit
  any `.worktrees/*` descendant.
- Missing `.pth` or `direct_url.json` files need no repair; an existing
  site-packages directory is still discovered structurally so a later write
  window can open it.
- Repair changes pointer content only. The write window restores every record
  and directory mode that existed before lifecycle code entered it.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[shared/shared.ava.okf.md]] — shared path and installation primitives
- [[cli/cli.ava.okf.md]] — public converge, start, and update surfaces
