---
type: doc
title: Delivery Watchdog Jobs
description: Delivery observation and stale-input retirement.
tags: []
---

# Delivery watchdog jobs

One resident loop alerts on stale pending delivery and retires stale claimed
or terminated-owner inputs to `done`. A running turn may legitimately hold a
queue behind it. Consuming a stale row does not certify successful execution.

There are no wake re-dispatch, poison budgets, persistent alert dedup,
resurrection, harvest or wedged-turn recovery jobs. Healthy hosts discover
fresh pending inputs through their ordinary polling and claim CAS. Message
senders retain finite same-key retries and the ordinary delivery-time attempt
to wake a terminated agent. Task notices retain their ordinary enqueue and
best-effort wake; see [[task-notifications.ava.okf.md]].
