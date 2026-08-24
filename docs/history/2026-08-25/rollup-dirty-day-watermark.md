# Rollup dirty-day watermark

## Decision

The events-maintenance rollup now treats Loki's count of its exact source
event families as a per-day dirty watermark. Missing, failed, count-changed,
and recent late-write days receive the existing full-day overwrite; matching
older days need only the count probe. The first pass after deployment rebuilds
the retained window once to establish state.

Maintenance owns a capacity-one Loki budget independent from the gateway's
user-facing query budget. Both use the same shared FIFO state machine, but
separate process-local capacity prevents background work from being mistaken
for shared admission. A pass deadline stops between probes or recomputes and
leaves remaining days dirty for the next hourly pass.

## Alternatives rejected

Recomputing every retained day on every pass was rejected because clean days
paid the full multi-query cost indefinitely. A recording rule was left to the
deployment layer: it can reduce query cost later, but does not replace durable
dirty state, bounded admission, or a whole-pass deadline. Sharing the gateway
singleton was also rejected because events maintenance runs in another process
and must not borrow user-read capacity by implication.
