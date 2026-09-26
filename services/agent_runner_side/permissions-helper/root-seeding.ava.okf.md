---
type: doc
title: Root seeding — the macOS permission ancestor
description: Durable per-home helper seeding and stop intent preserve signed-helper ancestry without foreign process signalling.
tags: [services, permissions-helper, lifecycle]
---

# Root seeding

Ordinary macOS start persists a private, fsynced seed containing the exact root
argv, working directory, run directory, log paths, and launch environment before
calling `root_seed`. The helper launches root as its direct child using
`posix_spawn` and `SETSID | CLOEXEC`; the session boundary leaves ppid unchanged.
Linux does not use this helper: [[services/ava_root/ava_root.ava.okf.md]].

The helper's LaunchAgent fixes `AVA_PERMISSIONS_HELPER_ROOT_SEED` to this home's
seed. After helper restart, a valid seed may resume an unexpectedly lost root.
Root's singleton flock and retained service custody independently refuse a
competing or uncertain generation. A held foreign lock means conflict: no
foreign PID is signalled, and there is no force override for that uncertainty.

`root_stop` first persists a private `root-stopped` intent, then sends TERM to its
owned root. The marker survives helper restart; explicit `root_seed` may clear it
only when no root is in flight. The helper never automatically escalates to KILL.
The CLI separately closes root-owned services and verifies native root death.
The keeper and named execution children share one native ownership lock. Spawn
and PID publication, ownership lookup and signal delivery, and native `waitpid`
with removal of the retained PID are indivisible under that lock. An exited child
cannot have its PID reaped and reused during a pending owned signal. The reaper
waits only retained root/session PIDs, leaving Foundation-owned children to their
own native owner; it never drains arbitrary children with `waitpid(-1)`.
`root_status` exposes state, owned PID, run directory, unexpected-exit restarts
and durable stop intent; while seeded it also reports the held seed's argv,
working directory, run directory and log paths (`seed`, advertised as
`ping.root_seed_report_v1`), never its environment, which carries secrets. A
macOS release compares that report, `seed.json` and the live root's kernel argv
to prove the pinned image ([[cli/release_transition/root_macos.ava.okf.md]]).
`ping.root_stop_intent_v1` and `ping.helper_shutdown_v1` are required before ordinary
start can seed this helper. An older protocol fails closed with an upgrade message.

An unreachable helper or ambiguous native job requires external diagnosis.
Converge cannot unload, kickstart, or replace its possible permission ancestor.
Normal stop and destroy use one exact-home retirement boundary. Its root and
broker sessions must be closed, native identity and plist must agree, and the
caller must be outside its tree. `helper_shutdown` closes root and execution
admission under the same child ownership lock, verifies native root custody is
free, then fsyncs a private `helper-stopped` intent before acknowledging. The
helper exits zero after sending the response. A lost response or helper crash
leaves the intent intact; a new helper exits zero before opening its socket or
seeding root. The LaunchAgent uses `KeepAlive.SuccessfulExit = false`: failures
restart it, but explicit acknowledged shutdown does not. External signals are
failures, not implicit stop requests.

The daemon dispatches socket requests serially. Its screen-capture child is a
Foundation `Process`, and the request waits for that child to exit before
returning. Shutdown therefore cannot acknowledge while a GUI child is in flight;
a stuck child causes a shutdown timeout, not a success. Once shutdown responds,
the server closes and exits before accepting another request. Panel mode runs
in a separate process. Any future concurrent request dispatch must preserve
this admission and drain boundary without stealing Foundation's native reaping.

The CLI waits for the captured native generation to exit, acquires the root
singleton lock, and rechecks service custody, unchanged home-bound plist, and
native job absence or positive idle status with retained shutdown intent before
removing the native job and definition. An interrupted retirement can finish
through this same path without starting the helper. Explicit force first revokes
native job restart authority and may then kill only its captured native owner.
An unreachable live helper or ambiguous custody still refuses retirement.

Start clears helper stop intent only after positive native job absence and then
registers the home-specific definition. It cannot clear intent on a loaded job
or replace a live ancestor during repeated start. Shared signed artifacts stay
untouched during retirement. Unknown custody preserves both the definition and
cluster reservation. These observations prove captured generations and exact
native job absence; macOS launchd does not provide a detached-domain closure
primitive for arbitrary escaped descendants.
