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
`<checkout>/.venv/.../site-packages/_editable_impl_ava.pth`. The prod pointer
must name a stable source root; a disposable worktree is never a legal target.
`shared/editable_install.py` owns the platform-independent discovery, exact-root
validation, repair, and temporary permission window used by lifecycle callers.

## Lifecycle flow

- `_converge` registers the prod assertion as a host-global step. It resolves
  the installed prod checkout, accepts that source plus explicitly allowlisted
  stable dev-clone roots, and repairs every other exact target to prod source.
  Repairs print a warning and emit the registered anomaly event
  `editable_pth_repaired` with the pointer, poisoned target, and source root.
- `converge_host` rejects host-global steps before execution in `.worktrees/`
  and `.claude/worktrees/` checkouts, so a worktree's own legal pointer is never
  inspected or rewritten. `ava start` inherits the same converge step.
- Update, recovery, rollback, and start's source-integrity auto-heal route each
  `uv sync` through `editable_pth_write_window`. A read-only pointer gains only
  owner-write for the sync; its exact original mode is restored in `finally`,
  including non-zero syncs and atomic replacement of the file.
- The native Windows deployment chain calls
  `python -m cli.commands._update_uv_sync`, giving it the same write window as
  the in-process POSIX/WSL paths. Discovery scans Windows `Lib`, POSIX `lib`,
  and `lib64` virtualenv layouts explicitly.

## Invariants

- Allowlisting is exact-root based. An allowlisted dev clone does not permit
  any `.worktrees/*` descendant.
- Missing `.pth` files are a no-op; the guard does not change Ava's editable
  installation mechanism.
- Repair changes pointer content only. The write window restores the mode that
  existed before lifecycle code entered it.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[shared/shared.ava.okf.md]] — shared path and installation primitives
- [[cli/cli.ava.okf.md]] — public converge, start, and update surfaces
