---
type: doc
title: Cluster view reads — curves, lanes, message edges
description: The three indexed reads behind the multi-agent view (cluster curves per time bucket, one lane per agent, message edges between agents) over a root agent's spawn and fork tree.
tags:
- services
---

# Cluster view reads — curves, lanes, message edges

## What is it
`GET /api/insights/cluster/{curves,lanes,messages}` (`services/derived/insights/cluster/`). Each takes the same selection — `root` plus `lineage` (`all` by default; `select_agents` in `base/telemetry/metrics/usage.py` walks the tree) — and a required window `from` / `to`. All three are indexed queries over event tables; none loads a checkpoint (the single-agent run timeline does, and the page asks for it only when a lane is opened).

## Curves (`curves.py`)
Buckets are epoch-aligned and sized by `window_seconds / buckets`, rounded up to a round width (1 s ... 7 d), so panning never moves a bucket edge. Only non-empty buckets are returned.
- **Cost**: sum of `cost_usd` of `llm_usage` rows (`telemetry_events`, index `(agent_id, ts)`) per agent per bucket, by the row's `ts` (the end of the call). The page shows it per minute (`cost * 60 / bucket_seconds`). A call without a recorded cost adds nothing and is counted in `unpriced_calls`.
- **Active agents**: agents with at least one `llm_usage` row in the bucket (`len(costs)`).
- **Messages**: `send_message` rows of `audit_events` in the bucket by send time, counted when both ends are in the selection. In that event `agent_id` is the receiver and `target_agent_id` the sender.
- **Queue time**: for those messages, `inbound_messages.claimed_at - created_at` (joined by the event's `inbound_id`), p50 and p95 per bucket by send time, over the claimed ones (`queue_samples`). `claimed_at` is when the receiver's claim step took the batch, the earliest the model can have read it; the exact per-message read time exists only in the receiver's checkpoint, which this view does not load. Unclaimed rows and rows pruned from `inbound_messages` are counted as messages but not as samples.

## Lanes (`lanes.py`)
One lane per agent in tree order (a parent before its children, siblings by birth; a parent is the fork source, else the birth spawner). A lane has:
- **Nodes**: `understanding_nodes` of one level intersecting the window, on their stored `start_ts` / `end_ts` (the run timeline re-derives node times from message blocks; the two differ by at most a block edge). The level is `level` if given, else `choose_level`: the level whose node count is nearest, on a log scale, to 8 per agent (at most 400 in all), ties to the coarser level. `levels` lists every level with nodes so the page can offer a switch. An agent with no node at the level has an empty row.
- **Bars**: `llm_usage` rows binned at `window / bins` and merged when closer than one bin. A request occupies `[ts - latency_ms, ts]`; one request is a bar of one call.
- **Events**: spawn, resurrect, restart and terminate rows of the audit record.

## Message edges (`messages.py`)
One edge per `send_message`: sender, receiver, sent time (the audit row's `ts`), read time (`claimed_at`, null while unclaimed), a 160-character preview. Both ends are the agent level; the read side says nothing about which message the receiver was handling or what woke it. At most `limit` edges, earliest first; `total` and `truncated` say what was cut.
