# Redis is loopback-only

## Context

A macOS network-stack defect prevents inbound TCP handshakes on non-loopback
addresses from completing for some listening processes, including every Redis
version tested. Connections never reach the accept queue, while loopback traffic
to the same process remains unaffected. The root-cause report is
`~/.ava/workspaces/3274/redis-kqueue-rootcause-20260824.md`.

The permanent off-box path is the host-level `com.ava.redis-bridge` service. Its
`/usr/bin/python3 relay.py` process accepts connections on the host's
private-network address and Redis port, then forwards them to `127.0.0.1` at the
same port.

## Decision

Ava always starts Redis with a loopback-only bind, regardless of whether the
cluster has a secret. Redis startup no longer waits for the host's reachable
address. This removes the competing non-loopback listener and makes the relay
bridge the single off-box ingress path (task #1469).

The converge firewall audit and declarative ALF manifest no longer include
`redis-server`, because it serves no off-box port. The bridge runs as an
Apple-signed built-in interpreter that ALF auto-allows. Regression tests pin the
literal Redis bind for clusters with and without a secret and prove that Redis
does not invoke the reachable-address wait.

Postgres and PgBouncer keep their loopback + reachable-address dual bind and
bounded startup wait. The shared `_bind_addrs` helper remains unchanged for
those paths.
