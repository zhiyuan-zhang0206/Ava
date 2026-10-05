# Immutable event-class resolution

## Decision

Event resolution is stored as class-level state in `event_dismissals`, not as a
mutation of historical Loki lines. A class is identified by category, level,
event name, source, and (for the future model) agent ID. Version one accepts
only the cluster-wide class (`agent_id IS NULL`), while retaining the nullable
field for the later per-agent extension.

## Consequences

The gateway records authenticated manual dismissals and reopens, then emits
transition markers. The gateway-owned maintenance daemon queries immutable Loki
lines in a fixed six-hour window, subtracts active class dismissals, and exports
absolute unresolved warning/error gauges. A ten-minute burst reopens an active
class when it exceeds the configured threshold. Automatic dismissal remains
off by default and operates only after a class is stable across daily samples.

The first implementation must query legacy JSON-only Loki streams without a
stream-label selector. That compatibility read is explicitly time-bounded to
`2026-08-30T11:10Z`; a follow-up can replace it after all relevant streams carry
the required labels.
