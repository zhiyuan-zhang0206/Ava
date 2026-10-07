# Run timeline: built from the message history and the understanding tree, not the event waterfall

Decision (2026-10-05): the single-agent run timeline reads only what the agent
persists about its own run — its message history (the checkpoint) and its
understanding tree (`understanding_nodes`). Every level of the tree is one row;
the bottom row, layer 0, is the deterministic division of the messages into units
(work = reasoning + tool call + result; text; inbound; note —
`base/agents/history/hierarchy/units.py`). The default window is the agent's
lifetime from its earliest message or node to its latest; a node or unit drills
to its own span, breadcrumbs step back.

Why: the turn rows were assembled from telemetry — `turn_end` as the skeleton,
`llm_usage` joined by span id or, failing that, by time window. That join is a
heuristic with a failure mode of its own (the "tracing data is unavailable"
warning), it needs a second bucket level for long windows, and it says nothing
the message history does not say better: the exact `usage_metadata` of every
AIMessage sums to the provider's `in_total`. A view whose navigation structure
is the understanding tree and whose bottom layer is the messages needs neither.
Costs are now plain sums over a node's message span, plus the node's own
generation cost from `understanding_chunk_calls`.

Audit events stay as optional lifecycle markers (spawn, restart, terminate); they
never decide the window. Nothing reads Loki.

Rejected: keeping the event waterfall as a second row set beside the tree (two
sources that disagree on what a "turn" is); linking a node to its generation
calls with a new `job_id` column (the call's request already records where the
chunk sat, so the node's stitched span is derivable and no migration is needed).
Cost accepted: a window read loads the stitched history (cached 5 seconds per
agent), and the whole-lifetime read of a very long-lived agent carries every
unit; narrowing the window is the relief.

Removed with it: the turn/bucket levels and their rows, the span/time-window
association, the strip, character axis, legend and message panel, the
`/run-timeline/message` route, the compare view (it stood on the same
waterfall; the cross-agent view is the next period's, built over this one's
nodes), and the settings `display.run_timeline_messages_max`,
`display.run_timeline_window_hours`, `display.run_timeline_compare_messages_max`
and `display.run_timeline_summary_visible`.
