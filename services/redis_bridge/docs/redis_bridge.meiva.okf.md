---
type: doc
title: Redis Private-Network Bridge
description: Root-supervised macOS TCP relay that keeps Redis loopback-only while serving authenticated split-cluster clients, with a bounded listener rebuild and end-to-end health observation.
tags:
- services
- redis
- infrastructure
---

# Redis Private-Network Bridge

The bridge is the macOS gateway host's off-box Redis ingress. Redis listens on
`127.0.0.1` only; the `redis-bridge` roster unit runs
`/usr/bin/python3 services/redis_bridge/relay.py` under the root supervisor. It
listens on the machine's declared private-network address at the cluster Redis
port and forwards bytes to the loopback listener. Redis authentication remains
end to end because the bridge does not terminate or interpret RESP.

`base.cluster.dataplane.redis_bridge.redis_bridge_endpoint` is the one answer to
"does this host need a relay, and where": macOS, a local data plane and a declared
non-loopback machine host. The roster builds the unit command from it, `services.supervision.roster.spec`
gates the unit out where it returns None, and the converge and health probes read
the same endpoint. The registry record owns the Redis port. The unit shares the
root's bounded restart budget with every other service; its identity probe sends a
RESP command through the listener and accepts any Redis reply
(`services.supervision.healthchecks.redis_bridge`). Converge only retires the
standalone launchd job and installed copy an earlier layout left behind
(`cli.commands.converge.redis_bridge`), so one supervisor owns the port.

The relay treats its listening socket as replaceable state. An `accept()` or
bind failure closes the descriptor and retries with a capped, growing delay, but
only `_MAX_LISTENER_FAILURES` (5) times in a row without an admitted connection:
the process then exits with status 1 and the root's restart budget decides what
happens next. The listener polls admission every quarter second, without applying
a timeout to connected sockets. Each successful bind and each rebuild failure is
timestamped on stdout, which the root keeps in the unit's log.

The five-second backend timeout bounds connection establishment only. Connected
sockets return to blocking mode so an idle Redis Pub/Sub subscription remains
open. Each connection has one pump in each direction. An orderly EOF shuts down
only the destination's write side, allowing the reverse stream to drain; the
connection owner waits for both pumps before closing their sockets. A transport
error shuts down both directions immediately, releasing a reverse pump blocked on an open
peer. A peer that only half-closes can intentionally keep reading indefinitely.

`RelayService` owns every accepted connection, including backend hostname lookup
and connection establishment. Each connection retains its handler, both actual
pump threads, and its sockets; completed connections are reaped during serving.
A listener rebind does not stop established connections. The standalone entry
point handles SIGINT and SIGTERM by closing admission and interrupting known
sockets, then uses one five-second stop budget to join connection handlers and
pumps. Connected sockets retain blocking backpressure throughout forwarding.

Socket transport/connect failures remain expected connection-local failures.
Unknown worker exceptions immediately print their traceback and remain attached
to the original service, whose `stop()` raises the first original exception.
A primary caller/listener or worker exception survives cleanup; every secondary
cleanup error is recorded by the original service where it is caught and remains
visible. A blocked hostname lookup cannot be interrupted by socket
shutdown: stop reports and retains unfinished connection owners within its
budget, and the same owner can collect their late completion/error on another
stop. The daemon workers permit the standalone process to exit after that
best-effort budget; this does not claim the blocked lookup was cancelled. A lookup
that returns after stop closes its backend without starting pumps.

The installed module remains pure stdlib and compatible with macOS's Python 3.9.
Component tests execute a copied source with `/usr/bin/python3`, including idle
listener polling, byte forwarding, and SIGTERM cleanup. The existing stdlib pump
model remains local to this relay: `socketserver` does not retain daemon request
threads for joining, while its non-daemon close can wait forever. If connection
fan-out or repeated lifecycle defects make this model costly, replace it with a
mature platform TCP proxy; a second consumer of this ownership pattern requires
extraction or a move to an established capability rather than another copy.

`meiva status` and the periodic cluster health probe authenticate and issue a
real Redis `PING` through the private-network endpoint: a live process or an open
TCP port alone does not prove that the relay can reach and serve Redis. Bridge
failures are alert-only and do not arm code rollback because a host listener
failure is infrastructure state.

## Entry Points

- `services/redis_bridge/relay.py:RelayService` — admission, connection ownership,
  finite stop, and original worker error collection
- `services/redis_bridge/relay.py:serve_forever()` — listener lifecycle and
  per-connection forwarding with cleanup on caller exit
- `services/supervision/roster/__init__.py` (`redis-bridge` unit) and `base/cluster/dataplane/redis_bridge.py` —
  rostered command and listen address
- `cli/commands/converge/redis_bridge.py:retire_launchd_redis_bridge()` — removes the
  standalone launchd job and installed copy
- `cli/commands/converge/redis_bridge.py:probe_redis_bridge()` — authenticated
  end-to-end PING

## Idle connection regression

`tests/test_redis_bridge_relay.py` uses a real TCP connection and a six-second
idle wait to cross the five-second connect timeout. A socketpair does not inherit
the timeout set by `socket.create_connection`, so it cannot reproduce this defect.
