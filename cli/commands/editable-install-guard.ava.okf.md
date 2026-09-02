---
type: doc
title: Editable Install Guard
description: Prod lifecycle assertions, execution gate, and update-time protection for Ava's editable install.
tags:
- cli
- lifecycle
- update
---

# Editable Install Guard

## What it is

An Ava editable install is a path pointer stored in
`<checkout>/.venv/.../site-packages/_editable_impl_ava.pth`, plus the editable
source URL uv records in the matching `<dist-info>/direct_url.json`. The
pointer's canonical form is exactly one allowed source-root line followed by a
trailing newline; a disposable worktree is never a legal target.
`shared/editable_install.py` owns the platform-independent discovery, exact-root
validation, atomic repair, and temporary permission window used by lifecycle
callers.

## Lifecycle flow

- `_converge` registers the prod assertion as a host-global step. It resolves
  the installed prod checkout, accepts that source plus explicitly allowlisted
  stable dev-clone roots, and repairs every non-canonical pointer. A legal
  stable dev-clone target is retained but normalized to one line plus newline;
  all other content is rewritten to the prod source root.
  The same pass asserts the `direct_url.json` records: a URL naming anything
  outside the allowlist (or a record that is unparsable or not marked
  editable) is repaired too, so the pointer and the URL can never disagree.
  Repairs print a warning and emit the registered anomaly events
  `editable_pth_repaired` / `editable_direct_url_repaired` with the path,
  poisoned target, and source root.
- The following host-global `prod editable exec gate` checks the remaining
  records and the virtualenv console script (`.venv/bin/ava` or
  `.venv/Scripts/ava.exe`). If a residual half-uninstall remains, it runs one
  recovery `uv sync` and rechecks. Finally it runs the checkout virtualenv
  interpreter in isolated mode from the platform temp directory to import
  `agent.exec_child`; success requires the printed module path to resolve under
  the prod source root. Any residual record, missing launcher, or failed import
  raises after a clear stderr diagnostic, so `ava start`, `ava converge`, and
  `ava cluster update` fail fast instead of accepting uv's false-success exit.
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
- The local, agent-runner, and dry-run update-chain sync sites use
  `run_uv_sync_verified`, which runs the same isolated import proof after uv
  exits successfully. Rollback and recovery remain on the plain sync seam
  because their trailing `ava start` runs the converge execution gate. Every
  sync command pins `--python` to the target checkout's virtualenv and removes
  inherited `VIRTUAL_ENV` from uv's child environment, so a foreign session
  cannot select another venv.
- The native Windows deployment chain calls
  `python -m cli.commands._update_uv_sync`, giving it the same write window as
  the in-process POSIX/WSL paths. Discovery scans Windows `Lib`, POSIX `lib`,
  and `lib64` virtualenv layouts explicitly.

## Invariants

- Allowlisting is exact-root based. An allowlisted dev clone does not permit
  any `.worktrees/*` descendant.
- Both missing editable records need no repair; an existing site-packages
  directory is still discovered structurally so a later write window can open
  it. A `direct_url.json` record without its `.pth` pointer is a half-uninstall
  violation, and the guard recreates the pointer at the checkout root. A pointer
  without a sibling direct-URL record is the reverse half-uninstall: the guard
  recreates the JSON only when the sibling `ava-*.dist-info` directory remains;
  if the directory is gone, it reports the violation and the recovery sync owns
  recreation because this layer cannot invent a distribution version.
- Repair changes editable-record content only. The write window restores every
  record and directory mode that existed before lifecycle code entered it.
- Session creation projects `VIRTUAL_ENV` only when its cwd is inside the
  spawning checkout; foreign-cwd Codex/worktree sessions retain the venv PATH
  prefix but omit that uv target-selection signal. Exec children make the same
  projection when their inherited cwd is outside the current interpreter root.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[shared/shared.ava.okf.md]] — shared path and installation primitives
- [[cli/cli.ava.okf.md]] — public converge, start, and update surfaces
