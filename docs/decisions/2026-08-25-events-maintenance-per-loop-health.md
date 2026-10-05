# Events-maintenance per-loop health

The events-maintenance daemon has three concurrent loops with different work units and deadlines. A single shared heartbeat was therefore not evidence that every responsibility was progressing: checkpoint trim or resolution could keep health green while dispatch was wedged, and dispatch itself refreshed the heartbeat while its unresolved worker thread was still running.

Health now aggregates three independent progress trackers. A tracker advances only after a bounded unit completes or during a known-bounded inter-run sleep; successful passes and errors carry wall-clock timestamps for diagnosis, while staleness uses monotonic time. A worker that exceeds its loop-specific hard deadline permanently wedges that tracker and parks the loop without retrying because the orphaned thread may still hold a database connection. The watchdog remains the recovery authority and removes the thread by replacing the process.

We rejected periodic beats during blocking work because they certify scheduling, not progress. We also rejected retrying after the deadline because two concurrent copies of the same pass could contend through an orphaned pool connection. The additive `/healthz.loops` payload keeps the existing identity/probe contract while making the failing responsibility visible.
