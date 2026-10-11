---
type: doc
title: Delivery Watchdog Jobs
description: Pending wake dispatch and delivery alerts, without automatic turn or terminated-owner recovery.
tags: []
---

# Delivery watchdog jobs

One resident scan loop observes delivery; it does not restart agents or recover
old work. `/healthz` reports scan progress. Its existing pending-wake dispatch,
backoff/poison bookkeeping and stale-row sweeps remain.

- Re-publish wakes for pending inputs of eligible idling agents after the normal
  dispatch threshold. Offline host verdicts and permanent provider rejection
  suppress background wake dispatch. No row's dispatch budget is spent while
  its host is unavailable.
- Alert on stalled pending delivery with `delivery_stalled`. Alert state survives
  service restarts. A running turn can legitimately hold a queue behind it.
- Sweep stale claimed and terminated-owner pending chat to `done` without replay.
  Queue consumption does not certify successful execution.

Terminated-owner persistent resurrection retry, crash-marked harvest RPC and
wedged-turn force/resurrect loops are removed. New chat delivery still attempts
ordinary home-machine resurrection; its sender uses finite same-key retries.
After a temporary failure, a new message or explicit operator resurrection can
try again. A permanent provider rejection stops background wakes immediately.
There is no billing batch-recovery tool.

Task notifications retain their ordinary durable enqueue and one delivery-time
wake attempt. They have no watchdog resurrection backstop. See
[[task-notifications.ava.okf.md]].
