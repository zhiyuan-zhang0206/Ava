# Post-checkout module-cache boundary

## Decision

An agent-runner self-update now replaces its Python image after checkout, uv
sync, and installed-SHA bookkeeping. The replacement image owns every
new-tree action: migration-layout validation, preflight, skill refresh,
quiesce, stop, and start. POSIX uses `execv` so the updater PID, handoff
identity, and inherited mutex remain continuous; Windows runs the continuation
as a child because its exec semantics do not preserve that handoff identity.

The gateway local leg uses the same boundary for its post-boot schedule-session
bounce. It invokes a new `_update_local` module entry in a fresh subprocess and
keeps its existing never-fatal behavior for a child failure.

## Rationale

The f22f5eb -> faf061d update loaded the old `shared.deploy_timing` before
checkout, then imported the new `ops.updater_outcome` during quiesce. The new
module required `STAGE_NO_PROGRESS_TIMEOUT_S`, absent from the old cached
module, so the rollout failed before the agent-runner reported its outcome.

Reloading or selectively evicting modules was rejected. It cannot establish a
complete dependency closure for code that a new tree imports lazily, and it
would leave stateful modules with mixed old/new identities. A process boundary
gives the post-checkout leg one coherent module graph; deterministic subprocess
tests preserve both the failure mechanism and the boundary's remedy.

The updater mutex fd is explicitly inheritable on POSIX. Python otherwise marks
new descriptors close-on-exec, which would silently drop the flock despite the
stable updater PID and allow a second updater into the continuation.
