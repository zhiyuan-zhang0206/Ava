---
type: doc
title: Fleet graph
description: Fleet graph reads, the poll cache, and Postgres failure behavior.
---

# Fleet graph

`GET /api/fleet/graph` reads agent nodes, token totals, and lifecycle edges
from Postgres under an 8-second statement timeout. The response can select a
time window and include terminated agents. A telemetry heartbeat guard
independently marks telemetry degradation.

Successful responses are kept in a 60-second Redis poll cache. Redis failures
fall back to direct reads. There is no longer-lived fallback copy: when the
Postgres read fails or times out, the exception reaches the gateway's
unhandled-exception handler and the request returns a logged, typed 500. The
fleet view's client keeps the previously fetched graph on screen and retries on
its next poll.

Route tests mount only this router with an explicit app-owned pool, bus and
telemetry guard, and the gateway's unhandled-exception handler. They exercise the
real PG read phase and substitute a failing pool to assert the 500.
