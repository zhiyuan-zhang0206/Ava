# Redis bridge convergence and listener recovery

## Context

The Redis bridge existed only as a manually installed
`$AVA_HOME/redis-bridge/relay.py` and `com.ava.redis-bridge` LaunchAgent. The
repository described that runtime but contained no generator or source for it,
so a fresh gateway could not reproduce the service and an update could not
repair it.

On 2026-09-05 the gateway host's launchd job still reported `running` with PID
10025, while the socket for `100.103.96.72:6380` was `CLOSED` and no listener <!-- tailnet-ip-ok: exact incident evidence -->
existed. The relay caught `accept()` errors, slept, and retried the same failed
descriptor forever. The last normal remote connection was at 09:16; a
company-mini connection was refused at 10:41. A launchd kickstart restored the
listener at 10:42:57 and the remote agent consumed its pending work immediately.

## Decision

The repository now owns the pure-stdlib relay source and prod gateway converge
owns both its stable installed copy and launchd job. The listener socket is
replaceable state: any accept or bind failure closes it and re-enters a capped
rebind loop, so a private-network interface cycle does not require a process
restart.

The operator surfaces do not infer health from the PID or a bare TCP connect.
`ava status` and the periodic cluster health probe authenticate and issue a real
Redis `PING` through the private-network relay. A bridge failure alerts as host
infrastructure and does not arm automatic code rollback. Redis itself remains
loopback-only; the bridge continues to carry the off-box boundary decided in
`docs/history/2026-08-24/redis-loopback-only.md`.
