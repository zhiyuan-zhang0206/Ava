---
type: doc
title: Editable Install Guard
description: Read-only editable-install inspection used by the exec boundary and the health probe.
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
`base/deploy/release/editable_install.py` owns the platform-independent discovery, exact-root
validation, atomic repair, and temporary permission window for explicit
installation tooling.

## Execution boundary

Ordinary start admits a `StartRuntime` before preparing host state. A retained
image is verified in place and never invokes editable repair, dependency sync,
or source convergence. Development starts keep host wiring and plugin scaffold
preparation, but do not discover or rewrite a separate production virtualenv.
Converge has no editable pointer repair, protection, or reinstall step.

`base/deploy/release/editable_install.py` provides read-only inspection of
editable installations: `editable_install_violations` discovers the pointer and
every `direct_url.json` record and judges them against the exact-root allowlist,
and `editable_console_script_violations` checks the launcher. The exec boundary
refuses a child spawn from a violating interpreter and the health probe reports
the violation; neither rewrites a record. The fix for a violation is the locked
dependency sync, run by the operator for the intended checkout and its
virtualenv (`cli/python_install.py` owns normal locked dependency acquisition and
editable builds). Discovery covers the `lib`/`lib64` layouts; a successful
package-manager exit alone does not prove that the install is usable, so the
health probe also asks the venv interpreter to import the application.
[development setup](../../../docs/conventions/dev-setup.md#development-in-a-worktree)
owns environment isolation and dependency commands.

## Invariants

- Allowlisting is exact-root based. An allowlisted dev clone does not permit
  any `.worktrees/*` descendant.
- Both missing editable records are clean. A `direct_url.json` record without
  its `.pth` pointer, or a pointer without a sibling direct-URL record, is a
  half-uninstall violation; this layer cannot recreate either, because it cannot
  invent a distribution version, so the recovery sync owns recreation.
- Inspection changes nothing: no file is written and no permission is changed.
- Session creation projects `VIRTUAL_ENV` only when its cwd is inside the
  spawning checkout and not below its `.worktrees/` or `.claude/worktrees/`
  directories; foreign-cwd and sibling-worktree Codex sessions retain the venv
  PATH prefix but omit that uv target-selection signal. Exec children make the
  same projection when their inherited cwd is outside the current interpreter
  root or below either sibling-worktree directory.

## Key dependencies

- [[commands.ava.okf.md]] — command-module and lifecycle overview
- [[base/docs/base.ava.okf.md]] — shared path and installation primitives
- [[cli/docs/cli.ava.okf.md]] — public converge, start, and update surfaces
