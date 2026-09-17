# Browser read-model repair

A reconnect is evidence of a possible event gap. Suppressing a second reconnect
inside a fixed cooldown can leave infinite-stale caches wrong indefinitely.
The browser now coalesces reconciliation work without suppressing its obligation:
events received during a read require another read after it completes.

This scheduler is deliberately a query-cache owner, not a durable event bus.
It uses authoritative domain reads and bounded per-active-key scheduling state;
it does not infer event ordering or persistence from transport arrival time.

Selected-detail ownership follows in
[selected-agent-stream-ownership.md](selected-agent-stream-ownership.md).
