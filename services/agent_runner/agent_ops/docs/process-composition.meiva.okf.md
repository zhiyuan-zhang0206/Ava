---
type: doc
title: Agent ops process composition
description: Executable-owned image, database admission, event producer, and local status propagation.
tags:
- services
- lifecycle
---

# Agent ops process composition

The graceful-stop child substitutes the served body while calling the actual
daemon `main()` entry. Its consumers cover normal cancellation and a wedged
executor; this is a shutdown composition check, not a live ops request test.

`services/agent_runner/agent_ops/daemon.py:main` rejects unknown argv, captures
one `LoadedCommit`, and retains a `CodeVersion` and non-exempt `ProcessDbGate`
for the `ops` process. Its database factory reads `db_config_from_boot(config)` at each
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
bind/auth posture, pidfile/endpoint resources and invocation executor/pool operations.
Handle/pool operations require the root's factory and `ConfigAuthority`; they retain their original read points, pool posture and
non-fatal registration behavior. The daemon calls that public owner directly,
without a private module import, forwarding wrapper or helper re-export.

- **Boot self-registration** (`boot.register_boot`): once the health server is up, the daemon calls `base.cluster.machines.register_self` with its explicit machine name, capabilities, description and home; `unit_dial_url` receives that owner's capabilities and host reader value for its own unit — clearing any `stopped_at` latch and restamping `up_since_at`. The `machine_units` row is a liveness record, so the process whose liveness it stands for is the one that writes it; `meiva start` alone could not, because a host also comes back via an OS autostart, a watchdog respawn, or a rollout's restart leg. Deliberately **non-fatal** (unlike `assert_schema_current`): a stale row is not incorrect dispatch, and exiting would hand the watchdog a respawn loop that takes the host dark for the gateway. `unit_dial_url` is shared with `meiva start`, so the two writers cannot advertise different addresses for one unit.

`serve_ops_daemon` builds its authority from this same boot's eager runtime. A restricted
profile retains that runtime while `complete_read_model()` supplies the full
validated read model at the original authority-construction point. Fresh file
reads remain pinned to the original `.env` path, and secret refusal and field
validation stay in the existing authority owner. No ambient runtime is substituted.

Pool/executor capacity and event-bus inputs read the supplied authority at each
original construction point. The semaphore reads the boot at startup; saturation
reads the same boot again for every health callback. Endpoint/pidfile operations
require that boot and the explicit runtime directory. Their public definitions
live in `boot.py`, with no forwarding aliases left on the daemon.

`serve_ops_daemon` passes its factory and image through each request and worker binding.
Pool construction, schema admission, pidfile and health timing remain in the
existing order. The health payload uses the entry image. The synchronous status
arm passes it through `ops.cluster.operations.cluster_status_op` into
`ops.cluster_status.status_snapshot`. `running_sha` is that image's SHA, including
an honest `None`; `head_sha` remains the separate live checkout probe. A moved
checkout or rewritten start bookmark cannot change the process's loaded image.

This is an executable composition change, not retirement of all Settings roots
or the repository's remaining ambient baseline. Unknown public-contract
components and existing private test dependencies remain visible to their audits.

The shared machine-token acceptance policy and model-catalog bootstrap remain
separate legacy readers. This change does not alter their security or initialization
contracts. It removes this component's direct ambient endpoint/database/bus builders,
not the three global Config baseline entries.

The ops entry serves one health/RPC server. It does not construct startup DB/bus
handles for a retired outbox loop or restore the old resident replay TaskGroup.
The original primary-error and resource cleanup contract now applies to that
single serving loop. Request-time lifecycle arms still borrow the explicit
factory and authority at their original construction points.

## User-context changes

The serving TaskGroup owns `to_user_watch.watch_to_user`. Every 0.5 seconds after
its preceding scan/publication, outside the maintenance stop window, it snapshots numeric, non-symlink workspace
directories and only their immediate `to_user.md` entries.
Scanning runs in a worker thread; it never walks repositories or `node_modules`.
Each target is identified with `lstat` device, inode, size, nanosecond mtime and
ctime, including the link itself for a symbolic target. Directory identity uses
only device and inode, so unrelated workspace writes do not publish events.

The initial snapshot publishes nothing. Later workspace additions/removals and
target changes publish at most one `ToUserChanged` per agent per scan. Polling
coalesces writes between scans; a target created and removed entirely between
scans has no persistent state to invalidate. Missing entries during a scan are
normal deletion races, as is a workspace becoming a non-directory; other
filesystem errors fail the watcher and propagate
through the serving TaskGroup. Cancellation asks an in-flight scan to stop
between entries. There is at most one scan in flight, with no detached scan loop.

This avoids registering thousands of native watch roots: on macOS, watchfiles
1.2.0's notify backend restarts an increasingly large FSEvents stream for every
root during synchronous construction, holding the GIL and starving ops health
and lifecycle admission. Its polling backend only compares second-resolution
mtime by default, which misses same-second writes and atomic replacements.

The authenticated `POST /to-user/resource` route reads explicitly referenced files
through the same worker pool, admission semaphore and maintenance accounting. It
revalidates the document link chain on the owning machine, returns binary bytes
and caps each resource at 20 MiB. It does not launch per-agent page servers.

Request receipt and configuration-write invariants live in the child request-ownership document.
