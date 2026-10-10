---
type: doc
title: Agent ops process composition
description: Executable-owned image, database admission, event producer, and local status propagation.
tags:
- services
- lifecycle
---

# Agent ops process composition

`services/agent_runner/agent_ops/daemon.py:main` rejects unknown argv, captures
one `LoadedCommit`, and retains a `CodeVersion` and non-exempt `ProcessDbGate`
for the `ops` process. Its database factory reads current settings at each
original handle-construction point while retaining that gate. Work and logging
therefore share the same admission owner rather than constructing independent
process identities.

The root owns a lazy `ClientSet` pipeline using that same database factory.
Logging receives its producer, the existing live machine reader, and the entry's
captured image. On exit the constructed writer stops within two seconds; a cold
close does not construct it. A close failure cannot replace an active startup or
service failure. The original async cancellation/drain and hard exit remain:
`Runner.close` must not join a wedged default executor before process exit.

`services/agent_runner/agent_ops/boot.py` is the definition owner for registration,
bind/auth posture and handle/pool operations. Database operations require the
root's factory; they retain their original read points, pool posture and
non-fatal registration behavior. The daemon calls that public owner directly,
without a private module import, forwarding wrapper or helper re-export.

- **Boot self-registration** (`boot.register_boot`): once the health server is up, the daemon calls `base.cluster.machines.register_self(url=unit_dial_url(machine_role()))` for its own unit — clearing any `stopped_at` latch and restamping `up_since_at`. The `machine_units` row is a liveness record, so the process whose liveness it stands for is the one that writes it; `ava start` alone could not, because a host also comes back via an OS autostart, a watchdog respawn, or a rollout's restart leg. Deliberately **non-fatal** (unlike `assert_schema_current`): a stale row is not incorrect dispatch, and exiting would hand the watchdog a respawn loop that takes the host dark for the gateway. `unit_dial_url` is shared with `ava start`, so the two writers cannot advertise different addresses for one unit.

`_main` passes its factory and image through each request and worker binding.
Pool construction, schema admission, pidfile and health timing remain in the
existing order. The health payload uses the entry image. The synchronous status
arm passes it through `ops.cluster.operations.cluster_status_op` into
`ops.cluster_status.status_snapshot`. `running_sha` is that image's SHA, including
an honest `None`; `head_sha` remains the separate live checkout probe. A moved
checkout or rewritten start bookmark cannot change the process's loaded image.

This is an executable composition change, not retirement of all Settings roots
or the repository's remaining ambient baseline. Unknown public-contract
components and existing private test dependencies remain visible to their audits.
