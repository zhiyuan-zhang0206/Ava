# Backend polling performance

Repeated dashboard polls were paying for remote health probes and identical
Loki aggregations even when the operational value of the answer had not
changed. We chose short, explicit staleness budgets at the semantic response
boundary instead of caching transport calls: status snapshots stay coherent
as one whole response, while event counts and attribute aggregates reuse only
when their complete filter shape and minute-aligned window agree.

The status path serializes misses because concurrent pollers otherwise turn
one expiry into several copies of the slowest remote-machine probe. Degraded
snapshots are cacheable too: retrying their failed component on every poll
would recreate the load spike the cache is meant to remove.

The Loki cache stores successful aggregate results only. A failed query must
remain observable and retryable, while grouped results cross the cache
boundary as copies so a caller cannot change later responses. A simple hard
bound with whole-cache replacement was preferred to an LRU: the short TTL
already governs usefulness, and eviction sophistication would add shared-state
complexity without improving the dashboard contract.

Gateway latency telemetry gained a tail percentile and per-route sample count
alongside the caching changes. The count is necessary context for interpreting
a tail value on sparse routes; the higher percentile makes regressions that do
not move p95 visible without treating a single maximum as representative.
