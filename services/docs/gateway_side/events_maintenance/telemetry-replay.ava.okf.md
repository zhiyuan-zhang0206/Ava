---
type: doc
title: Telemetry Events Replay
description: Hourly replay of this machine's JSONL mirror into the telemetry_events table.
tags:
- observability
---

# Telemetry Events Replay

`services/upkeep/events_maintenance/telemetry_replay.py`. The emitter appends a telemetry and log
batch's persisted events (`is_persisted`: `EventSpec.persist`, any warning-or-higher level, any unregistered name) to `telemetry_events` and writes the local JSONL mirror first (`base/telemetry/event_store.py`);
this pass repairs what the table missed: a database that did not answer, and the daemons that hold
no database login (gate, memory search, the browser daemons).

Every hourly pass reads this machine's full mirror files (`events-YYYYMMDD.jsonl`, never the rollup
or lineage files), newest first, from a saved byte position and appends their persisted telemetry and log rows
(the same `is_persisted` judgment as the live sink)
by event id (`ON CONFLICT DO NOTHING`), so a row the live writer already stored is skipped and a pass
can be repeated. The position is saved with the rows in one transaction, in
`agent_metric_file_cursors` under a `telemetry_events:` key (host and path; a replaced file restarts
from zero). A partial final line is left for the next pass. The pass has a 120-second budget and a
failure never blocks the other maintenance slices.

Another machine's mirror is not read here: the daemon on that machine replays it.
