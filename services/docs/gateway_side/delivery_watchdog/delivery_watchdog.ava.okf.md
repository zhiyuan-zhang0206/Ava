---
type: doc
title: Delivery watchdog
description: Observe stalled pending delivery and retire stale inputs without dispatching work.
tags: []
---

# Delivery watchdog

One gateway scan loop observes stalled pending messages and retires stale
inputs without re-publishing wakes or restarting agents. The agent host owns
ordinary pending-input polling; Redis wakes are latency hints and can be lost.
Queue claims still serialize input acceptance.

`AVA_DELIVERY_WATCHDOG_ENABLED` controls the daemon; its thirty-second default
scan interval is an observation cadence, not a recovery deadline. `/healthz`
reports loop progress. A process-local alerted set avoids repeat alerts while
a row stays pending, but does not survive a restart. Alert-layer rules own
cross-restart debounce. Old dispatch/poison and dedup table rows are left in
place; the runtime no longer reads or writes them.

Entry points are `services/wake/delivery_watchdog/daemon.py` and
`services/wake/delivery_watchdog/dead_letter.py`. See
[[services/docs/gateway_side/delivery_watchdog/jobs/jobs.ava.okf.md]] and
[[agent/db/docs/db.ava.okf.md]].
