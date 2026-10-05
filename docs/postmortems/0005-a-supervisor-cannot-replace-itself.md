# 0005 — A supervisor cannot replace itself

**Date:** 2026-08-24
**Anchors:** `shared/os_cron.py`, `tests/shared/test_os_cron.py`

## Summary

A failed rollout entered automatic rollback and restored code and schema, but
stopped during `ava start`. Health-probe registration unconditionally unloaded
the same launchd job whose descendant was performing the rollback, so launchd
terminated recovery before its cleanup could resume the host and release the
update lease. Registration now defers its own replacement, and a regression test
pins that ownership boundary.

## Timeline

- A cluster rollout advanced the pin, then finished incomplete after one runner
  failed its dependency-sync leg.
- Repeated gateway liveness failures triggered the health probe's automatic
  rollback to the last-known-good commit.
- Rollback reversed the new migration, reset the checkout, synchronized the
  environment, and entered `ava start` to restore services.
- Converge reached health-probe registration and issued `launchctl bootout` for
  the currently executing LaunchAgent. The rollback process tree disappeared
  before its `finally` block ran, leaving a paused host and a lease owned by a
  dead process.
- Recovery aligned the host with the existing cluster pin, reapplied the
  migration, restored services, and cleared the stranded lease with
  `ava cluster recover`.

## Root cause

`shared.os_cron._register_macos` treated every registration as an external
replacement: write the plist, boot out the label, then bootstrap it. A health
probe launched by launchd inherits its label as `XPC_SERVICE_NAME`; the rollback
and `ava start` descendants inherit it too. Booting out that label terminates the
owned process tree, including the code attempting recovery.

The rollback cleanup was correctly placed in `finally`, but an external
scheduler termination prevents Python from reaching it. The update lease made
the orphan visible and recoverable but could not make the killed process finish.
Existing `os_cron` tests covered legacy-label scoping, crontab safety, checkout
anchoring, and deregistration. None executed macOS registration from the target
job's own scheduler identity. The function's “idempotent” description further
hid that every call performed a destructive reload.

## Guardrails added

- `_register_macos` compares `XPC_SERVICE_NAME` with the current home's
  path-only and legacy health-probe labels. A match performs no plist write and
  no launchctl operation, preserving both the recovery process and detection of
  a pending spec change or relabel.
- `test_register_macos_never_reloads_its_own_launchd_job` recreates the inherited
  scheduler identity and asserts the old plist and process boundary remain
  untouched.
- `test_register_macos_never_relabels_its_own_legacy_launchd_job` protects the
  path-only label cutover from cleaning up the currently executing legacy job.
- `test_register_macos_still_reloads_from_another_launchd_job` proves the guard
  does not suppress convergence when `ava start` belongs to boot autostart.

The tests model launchd at the subprocess boundary; CI does not run a real macOS
LaunchAgent lifecycle. The exact scheduler-identity contract therefore remains
an operating-system assumption, observed on the affected host and isolated in
one comparison.

## Lessons

- A recovery actor must not destructively converge the supervisor that owns its
  process tree.
- Deferral must preserve evidence of pending work; writing the desired spec
  without loading it makes a later content check report a false convergence.
- A `finally` block protects against language-level exits, not an external
  supervisor terminating the process.

The general rule is condensed in
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).
