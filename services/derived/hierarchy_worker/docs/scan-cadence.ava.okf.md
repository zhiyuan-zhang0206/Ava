---
type: doc
title: Hierarchy worker scan cadence
description: Reconcile scan timing belongs to the resident schedule host.
---

# Hierarchy worker scan cadence

The event trigger enqueues compact-boundary jobs. Each cron tick drains due
jobs; a low-frequency reconcile scan covers missed events and stranded retries.

`schedules/hierarchy-worker-schedule.py` creates one `FallbackScanCadence` in
`main()`. Its slot callback passes that instance through `roots.tick()` to
`runner.run_tick()`. Configuration is read for each tick, while the last
successful scan timestamp persists for the lifetime of this schedule host.

The first tick scans. Later ticks scan once the configured fallback interval
has elapsed. A failed scan leaves the timestamp unchanged, so the next tick
retries; a completed scan advances it before job draining. A new schedule host
starts with a fresh cadence. Disabling the worker or entering maintenance does
not advance the scan timestamp.
