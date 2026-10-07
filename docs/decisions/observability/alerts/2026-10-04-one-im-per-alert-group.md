# The ingest sends one IM per alert group, not per instance

## Context

The policy groups the instances of one rule into one webhook POST (`group_by [alertname]`), but the
ingest still sent one IM per instance, so a fleet-wide condition (five machines offline, a vendor's
stall burst) paged as many messages as instances. The user asked for like alerts to arrive as one.

## Decision

1. One webhook POST is one notification group. The ingest sends one IM per (status, alertname) in
   it: a lone instance keeps the single-alert format; several share one head (highest severity, the
   alertname, `xN`), list the first three summaries and count the rest.
2. The store is unchanged: one row per (fingerprint, starts_at), each stamped `notified_at` when the
   group's message lands. A failed send stamps none, and the next re-send of the group retries it
   whole.

## Alternatives rejected

- **Merging in the policy only.** Grouping already bounds the POSTs; without a grouped message the
  user still hears every instance.
- **One row per group.** The row is the unit of resolution and dedup; reconciliation and the
  alert UI read instances.

## Consequences

- A group's message names at most three instances; the rest are a count, the list is the alerts view.
- An instance that joins an already-announced group arrives as its own group message, at the
  policy's `group_interval`.
