# 0006 — An editable install is a cross-checkout pointer

**Date:** 2026-08-25
**Anchors:** Task #1572 (summarized); `shared/editable_install.py`;
`cli/commands/_converge.py`; `tests/shared/test_editable_install.py`;
`tests/cli/test_update_pth_permissions.py`

## Summary

A worktree command inherited the prod `VIRTUAL_ENV`, so `uv pip install -e .`
rewrote prod's `_editable_impl_ava.pth` to name the disposable worktree. Deleting
that worktree left every prod interpreter importing through a dangling pointer;
all agents lost `execute_code` for 12 minutes. The lasting rule is that an
editable install belongs to whichever virtualenv is active, not whichever tree
the shell is in. Prod lifecycle convergence now asserts and repairs that pointer,
and worktree operations must clear `VIRTUAL_ENV` explicitly.

## Timeline

- A development command ran from a worktree while its process environment still
  carried the prod virtualenv.
- `uv pip install -e .` updated prod's `_editable_impl_ava.pth` to the worktree
  path. The worktree still existed, so imports continued and the corruption was
  latent.
- Routine worktree cleanup deleted that path. Prod imports then failed with
  `ModuleNotFoundError: agent`, breaking every agent's `execute_code` channel on
  the machine.
- The pointer was restored to the prod source root; service recovered after a
  12-minute outage.
- Task #1572 added the lifecycle assertion, update permission tolerance, tests,
  and the worktree operating rule.

## Root cause

The isolation model covered homes, data planes, service checkout anchors, and
worktree-local virtualenvs, but it treated editable installation as if `cwd`
selected its destination. It does not: `VIRTUAL_ENV` selected a different,
long-lived environment, and editable installation mutates a plain path file
inside that environment. The worktree and prod therefore shared one mutable
import-authority seam despite being isolated everywhere else.

The safety nets missed it independently:

- No converge/start assertion compared prod's `.pth` content with the installed
  source root, so the corrupt state was valid to every lifecycle check.
- Worktree guidance said to use the local `.venv` but did not require clearing an
  inherited `VIRTUAL_ENV`, leaving correctness to shell history.
- Cleanup checked live processes anchored under a worktree, but not long-lived
  virtualenv pointers that would become dangling after deletion.
- Tests established that a worktree's own `.pth` was legal; none injected a
  worktree target into a long-lived prod virtualenv and proved it was repaired.
- Making the pointer `0444` contained accidental rewrites but conflicted with
  legitimate rolling `uv sync`, so it could not stand alone as the permanent
  mechanism.

## Guardrails added

- `shared/editable_install.py` finds both POSIX/WSL and Windows virtualenv
  layouts, accepts only exact stable roots, repairs poisoned prod pointers, and
  emits the warning-level `editable_pth_repaired` event.
- The guard is a host-global converge step. `ava start` runs the same step;
  worktree converge skips it before it can touch the worktree's legal pointer.
- Every Python update/recovery/rollback sync uses one permission window, and the
  Windows native update chain reaches the same seam. The exact original mode is
  restored in `finally`, including failed syncs.
- Regression tests cover a missing target, a real worktree below an allowlisted
  dev clone, a Windows-layout prod virtualenv, worktree isolation, successful
  update/recovery, and failed agent-runner sync.
- The runbook requires `env -u VIRTUAL_ENV` and a long-lived `.pth` inspection
  before worktree deletion.

The dev clone's own long-lived virtualenv is not auto-repaired by prod converge;
the pre-deletion inspection is its guard. The rollout that first delivers this
protection is also governed by
[the first-rollout rule](0001-a-rollout-cannot-deliver-its-own-protection.md):
hosts already protected with `0444` need the operator to open that mode for the
old updater that installs this change. Later updates restore it automatically.

## Lessons

- A mutable pointer crossing an isolation boundary defeats the directories and
  databases isolated around it; assert the pointer at the long-lived consumer.
- Environment-selected tools need explicit environment hygiene. `cd` is not an
  authority boundary.
- Before deleting a source tree, inspect durable references to it, not only live
  processes currently anchored under it.
- Write protection is a containment layer, not an update strategy; legitimate
  mutation needs a narrow, exception-safe permission window.

The general rule is condensed in
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).
