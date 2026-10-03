# Self-evolution labeler normalization (task #1289)

## Decision

The self-evolution labeler now treats repeated execution failures as a rate
signal after a short-run floor: `max(3, ceil(turns / 10))`. The three-failure
floor preserves the existing short-worker failure signal, while the 10% rate
prevents completed long-running workers from being labeled fumbled solely for
normal exploratory iterations.

The collector marks a chat inbound as broadcast when an identical
source/content pair was delivered to more than one agent during its collection
window. The record builder removes such fleet-scoped instructions from the
per-agent correction, re-prompt, and peer-feedback signals. Direct children of
`TEST-` orchestrators are also excluded from the health dataset.

## Consequences

- The daily row for agent #4731 (204 turns, 13 failed executions) no longer
  meets the execution-failure fumble threshold, while a five-turn worker with
  three failures still does.
- A targeted correction continues to produce a correction signal; only actual
  fan-out messages are excluded.
- Test-only orchestration workers no longer contribute artificial failures to
  production self-evolution health metrics.
