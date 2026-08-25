# Permission watcher alert delivery

## Context

The permission watcher had already stopped writing agent-owned notices, but a
pure local detector left system permission prompts without a user-facing system
channel. The gateway alerts ingest already provided persistent UI visibility,
firing/resolved instance reconciliation, and IM fan-out without agent ownership.

## Decision

The watcher posts permission prompt lifecycles to the loopback alerts ingest.
Alert identity is permission kind plus the responsible application shown by
macOS; a varying triggering tool is annotation text only. Resolution replays the
firing instance's original `startsAt`. Delivery retries once and has no direct
database fallback.

Two local silence layers bound recurrence noise. A pending incident never emits
a second successful firing, and a recurrence within 12 hours after resolution
is tracked in silent mode without firing or resolved delivery. The unresolved
alerts row replaces the former 30-minute escalation machinery.

## Consequences

Permission prompts are visible in the alerts list and bell, and the alerts
fan-out sends one firing and one matching resolved IM for a full incident.
Nothing writes `agent_notices`, and a gateway delivery outage degrades to a
warning log rather than a database bypass.
