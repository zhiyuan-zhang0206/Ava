---
type: doc
title: Run Timeline Reads
description: The understanding tree and the layer-0 message units over a window, for the single-agent run timeline.
tags:
- gateway
---

# Run Timeline Reads

`GET /api/agents/{id}/run-timeline?from&to` serves a window over the two things an agent persists about its own run: its message history (the stitched checkpoint, `load_checkpoint_history_full`) and its understanding tree (`understanding_nodes`). Nothing is derived from telemetry events and nothing is read from Loki.

- **Window.** Without `from`/`to` it is the agent's whole lifetime: the earliest message or node to the latest (read times) (`lifetime` in the response). Audit events, the agent row and inbound rows never move it. An agent with neither falls back to the last 24 hours and `lifetime: null`. Drilling is asking for a node's or unit's span as the window.
- **`nodes`.** Every tree level intersecting the window (on read times), none capped. `level` is the engine level (1 = leaves), stable across windows. Each node carries `span_start..span_end` (stitched message indices), its full `summary`, `usage` (the AIMessage `usage_metadata` summed over the span: calls, input, cache read, output; `hierarchy/usage.py`) and `generation` (calls, tokens and seconds of the understanding calls that wrote it, from `understanding_chunk_calls`; None without a record). A call is matched to its node by where its request put the chunk, so node rows hold no job id.
- **Times are read times.** A message's time is when the model read it, never before the message before it. A message's own time is `message_read_time` (`ava_picked_up_at` when it records the pickup); an older message has only `ava_created_at`, its arrival, which can be earlier than its predecessor's (an inbound message stamped on arrival while the agent was still streaming) and would overlap neighbours on the axis, so its read time is rebuilt as the latest arrival so far in message order (`units.read_times`). Every time served (unit, node, window, lifetime) is a read time, and a node sits on the read times of the first and last message of its span (a parent spans exactly its children). Data that records the pickup passes through unchanged; older data gets its read order back here. The stored node `start_ts` / `end_ts` and the raw message stamps are not rewritten and not served (the raw message detail keeps its own `ava_created_at`).
- **`units`.** Layer 0 as the timeline draws it: the deterministic message units of `hierarchy/units.py` (inbound, text, note) with each work unit split into three blocks by `display_blocks`: `thinking` (the model's generation for the turn, from the read time of the previous message to the end of the stream; when the turn also has text and a recorded reasoning time `ava_reasoning_ms_by_block`, it ends after that time and the `text` block takes the rest of the stream), `call` (an instant at the end of the stream) and `output` (from the end of the stream to the tool result). A turn of only text spans its stream. Each block carries its own `kind`, extent, the message span of its unit (`i0`..`i1`) and a preview. Grouping and the catalog are not affected: a work unit stays one unit there. Blocks overlapping the window and carrying a read time are served.
- **`events`.** Optional lifecycle markers (`spawn`, `resurrect`, `restart_completed`, `terminate`) from `audit_events`, paged oldest first; a failed read leaves them out.

`GET /api/agents/{id}/run-timeline/messages?start&end&limit&full` returns raw messages by stitched index (at most `limit`, `next_start` continues), split into parts by the console timeline's projection; parts longer than `display.run_timeline_message_text_max` are clipped and flagged unless `full=true`.

`HistoryViewCache` (`history.py`, on `app.state.run_timeline_views`) keeps each agent's derived view (history, units, usage sums) for 5 seconds so a drill's burst of requests costs one checkpoint read; a read that finds a node past the cached history asks for a fresh view. A node span outside the history is an explicit error.
