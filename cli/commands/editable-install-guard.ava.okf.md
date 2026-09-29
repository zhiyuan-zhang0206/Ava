---
type: doc
title: Editable Install Guard
description: Explicit editable-install inspection and permission restoration for development tooling.
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
pointer is legal when every non-empty line names an allowed source root; a
disposable worktree is never a legal target. uv writes one line per wheel
package, so its native multi-line form (including repeated roots and no trailing
newline) is healthy.
`shared/editable_install.py` owns the platform-independent discovery, exact-root
validation, atomic repair, and temporary permission window for explicit
installation tooling.

## Execution boundary

Ordinary start admits a `StartRuntime` before preparing host state. A retained
image is verified in place and never invokes editable repair, dependency sync,
or source convergence. Development starts keep host wiring and plugin scaffold
preparation, but do not discover or rewrite a separate production virtualenv.
Converge has no editable pointer repair, protection, or reinstall step.

`shared/editable_install.py` provides explicit inspection and repair tools for
editable installations. Its write window opens the exact records and structural
site-packages, dist-info and launcher directories, then restores their original
modes. A caller must identify the intended checkout and its virtualenv; automatic
startup does not grant permission to repair another installation. The
`cli/python_install.py` source dependency pipeline owns normal locked dependency
acquisition and editable builds.

The import proof runs the selected virtualenv interpreter in isolated mode from
a temporary directory and checks the actual `agent.exec_child` path. A successful
package-manager exit alone does not prove that the install is usable. Discovery
covers POSIX `lib`/`lib64` and Windows `Lib` layouts. See the runbook's manual
editable-install recovery procedure for an explicitly selected installation.

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
  record and directory mode that existed before the explicit write window opened.
  This is exception-safe permission restoration, not a filesystem transaction:
  process termination or an OS refusal to restore permissions still requires
  operator recovery. Directory protection prevents launcher unlink/replacement;
  it does not recursively protect dependency files from in-place writes.
- Session creation projects `VIRTUAL_ENV` only when its cwd is inside the
  spawning checkout and not below its `.worktrees/` or `.claude/worktrees/`
  directories; foreign-cwd and sibling-worktree Codex sessions retain the venv
  PATH prefix but omit that uv target-selection signal. Exec children make the
  same projection when their inherited cwd is outside the current interpreter
  root or below either sibling-worktree directory.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[shared/shared.ava.okf.md]] — shared path and installation primitives
- [[cli/cli.ava.okf.md]] — public converge, start, and update surfaces
