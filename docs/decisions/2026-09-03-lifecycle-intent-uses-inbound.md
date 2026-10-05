# Lifecycle intent uses the existing inbound command identity

## Decision

Reuse `inbound_messages.id` and `claimed_at` for command identity and durable
acceptance. An accepted restart/terminate records its exact runtime generation
and owner; one same-agent foreign-key pointer in `agents_meta` serializes work.
Later commands remain pending, in ID order, instead of overwriting the pointer.
Redis remains a wake hint, not the durable record.

`applied_at` and `observed_at` are distinct facts. Neither is stamped by mere
acceptance. A stale target can close as `payload.lifecycle_result` with only
`outcome=superseded, reason=target_replaced`; it is not an applied effect.
Identity and targets are not duplicated into this payload. Legacy rows retain
NULL facts. Unknown ownership does not prove replacement.

## Alternatives

Payload-only intent cannot enforce same-agent references or protect unfinished
commands from retention. A second durable queue duplicates ordering, retries,
identity and retention. A boolean in hosted cache disappears on host crash and
does not identify which incarnation it can affect.

## Staged implementation and remaining boundary

The additive schema and transaction helpers are inactive foundations. They do
not change dispatch or advertise a protocol. Before activation, the remaining
work must replace early `done` lifecycle claims, `_flip_to_restarting`'s separate
status writer, hosted `restart_requested`-only intent, and self-respawn's second
action decision with the single pointer's target-fenced apply/observe path.
Reserved result payload input must be rejected at ingress; it is not yet a
trusted public result contract. No CLI/UI may call accepted work completed.

Tests first cover rollback before acceptance, repeated acceptance after caller
loss, serial pending commands, retention protection and stale-target closure.
Effect-before/after-crash and observation proofs require actual integration,
not a mocked acknowledgement. External process effects are not exactly-once.
All old unconditional writers require a verified shutdown/upgrade barrier.

Rollback refuses while any new receipt or pointer exists rather than silently
discarding evidence old code cannot replay. No existing row is backfilled.
