"""Deployment and unit lifecycle: holds, deploy state, timing.

Sub-packages:

- ``git`` — git provenance of the running checkout and the memory pool's git ops.
- ``release`` — config-free loaded-runtime identity, the startup-input digest,
  verified reads, lock and collector acquisition and the editable-install guard.
- ``maintenance`` — explicit maintenance holds: admission, pause ownership,
  restart cohorts.
- ``lifecycle`` — local unit lifecycle state: serving generations, the desired
  service set, the status journal, the home lifecycle mutex.
- ``state`` — durable deploy state: the per-host deploy posture.

The top-level modules are the deploy clock family: ``timing`` (the clock lattice
every ordered timing constant registers in), ``progress_timeout`` (the one
"stopped making progress" timeout and the deploy-family readiness and agent-lease clocks), ``stop_timing``
(host cancellation diagnostics) and ``transition`` (time-graded severity for bounded
transition windows).

This door is docstring-only; import the member module you need.
"""
