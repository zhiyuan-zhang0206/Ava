"""Deployment and unit lifecycle: release images, publication, holds, deploy state, timing.

Sub-packages:

- ``git`` — git provenance of the running checkout and the memory pool's git ops.
- ``release`` — config-free release-image verification, offline preparation,
  loaded-runtime identity, verified reads, lock and collector acquisition.
- ``writers`` — managed-writer publication evidence, observation and admission.
- ``updater`` — retained updater handoff and recovery evidence.
- ``maintenance`` — explicit maintenance holds: admission, pause ownership,
  restart cohorts, straggler settle.
- ``lifecycle`` — local unit lifecycle state: serving generations, the desired
  service set, the status journal, home lifecycle mutexes.
- ``state`` — durable deploy state: the cluster deploy lease, host deploy
  posture and updater lease, the legacy cluster pin.

The top-level modules are the deploy clock family: ``timing`` (the clock lattice
every ordered timing constant registers in), ``progress_timeout`` (the one
"stopped making progress" timeout and the lease rule around it), ``stop_timing``
(host cancellation diagnostics), ``transition`` (time-graded severity for bounded
transition windows) and ``rollout_telemetry`` (settle-hold telemetry).

This door is docstring-only; import the member module you need.
"""
