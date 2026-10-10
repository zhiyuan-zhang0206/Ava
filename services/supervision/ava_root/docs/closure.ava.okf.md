---
type: doc
title: Ava Root service stop
description: Bounded best-effort group signals and the direct-child exit used by ordinary service replacement.
tags: [services, lifecycle]
---

# Ava Root service stop

Root launches application services as its direct children. Each POSIX child
leads a process group (`process_group=0`) in root's own session, preserving the
macOS signed-helper permission ancestry. The captured `OwnedProcess` birth
protects ordinary signaling from a PID now naming another process.

Stop sends TERM to the known leader's group only while that birth remains live
and still leads that group. A leader no longer in its original group receives
an identity-checked direct signal. Root never signals its own process group.
Concrete identity and OS failures propagate; an already-gone process is normal.
There is no process-table scan, descendant capture, durable spawning record,
custody reconciliation, or proof that a former group is empty.

Root waits for its existing child watcher within the manifest's
`stop_timeout_s`, or `SupervisorConfig.stop_timeout_s` (10 seconds) when absent.
The service roster derives declared windows from each service's own stop ceiling
plus `STOP_MARGIN_S` in `services/supervision/ava_root_glue/manifests.py`.
A live child that exceeds its window raises a concrete failure. Only explicit
force permits KILL, after that window, with a second bounded child wait.

The watcher reaps the direct child, publishes its exit and drops that generation.
An unexpected exit is eligible for the existing health scheduler's replacement;
no historical custody file blocks spawning. Operator intent, input-seal validation
and recorded restart failures retain their existing owners.

Supervisor retains every actual watcher Task across generation replacement,
retrieves its completion, and immediately records an unknown original error.
That error remains at this Supervisor and is raised at shutdown; it does not
cancel other root participants. `exited` certifies a completed native reap and
is never set by an error or cancellation cleanup path. Shutdown closes birth
admission, attempts every child stop, and observes its retained watchers for an
independent finite join budget (0.2 seconds by default). A refused child's watch
continues to observe its real reap. `unfinished_watches` exposes the actual live
Task identities; a bounded return does not certify their termination. Repeated
shutdown can collect a late error from the same original owner. Multiple
failures retain their original objects, including an existing operation failure.

Group signaling is best effort. A child forked during the leader's TERM handler,
a process that moved to another group/session, or descendants left after a root
crash may remain. Their disappearance is not a service-replacement requirement.
The root instance lock still prevents two live supervisors for one run directory;
it is not a crash-recovery process inventory.

Daemon shutdown attempts bounded child closure once and propagates failures. It
does not wait indefinitely for another stop request or retain a cold-start lock
for descendants. Native tests cover parentage, group TERM, direct-child timeouts
and force, PID reuse, ordinary replacement, and explicitly cleaned late children.
