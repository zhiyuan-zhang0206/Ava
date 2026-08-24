# Redis healthcheck respawn

## Decision

The gateway watchdog's Redis ACL healthcheck now repairs an unreachable local
Redis by calling `_start_redis`, the same idempotent bring-up primitive used by
`ava start`, with the Redis port read from the cluster registry. It verifies the
cluster identity can PING after bring-up before reporting recovery.

## Rationale

Redis is the cluster message bus. Leaving `ConnectionError` and `TimeoutError`
to a future manual `ava start` made a killed Redis remain unavailable
indefinitely. Reusing the existing bring-up preserves its listener, secret, ACL,
and readiness semantics rather than creating a second repair path.

No-secret clusters skip reachable-server ACL re-affirmation, but retain the
liveness and respawn path because their Redis outage has the same message-bus
impact.
