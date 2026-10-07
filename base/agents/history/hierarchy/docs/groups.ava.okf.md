---
type: doc
title: Upper-level Grouping
description: How the understanding tree grows above level 1 — every so many new open nodes of a level (60 at level 1, fewer higher up) are offered to one LLM call that closes groups into the next level, checked by id and corrected in the same conversation.
tags:
- hierarchy
- understanding
- groups
---

# Upper-level Grouping

Level 1 of `understanding_nodes` is written by the chunk calls, which group layer-0 message
units themselves
([[base/agents/history/hierarchy/docs/chunks.ava.okf.md|Chunk-triggered Understanding]]).
Everything above is grouping, one standalone call per check, because a chunk call sees only its
own segment's messages: a level's **open** nodes (no `parent_id`) are grouped into nodes of the
next level. Code: `group.py` (prompt, reply rules,
conversation), `group_store.py` (cursor, groups, call record), `group_consumer.py`
(when, and the run). The run-timeline serves every level as it does the leaves.

## Trigger

The consumer loop calls `run_group_checks` after a chunk job is `done`. It
walks up from level 1 and stops at the first level that closed nothing. A level is
due when its open count is at least `check_threshold(level)` above
the count at its previous check: `AVA_UNDERSTANDING_GROUP_CHECK_OPEN` (60) at level 1, divided by `AVA_UNDERSTANDING_GROUP_CHECK_DECAY` (3) once per level above and never below 6 (60, 20, 7, 6, ...), because a higher level fills far more slowly and one threshold would leave the top days behind — so one call can close several groups; kept durably in `understanding_group_state.last_checked_open`, so
one count is never checked twice. When groups close, the count that stays open
becomes the baseline; a declined or failed check records the count it saw. The same
row carries a lease (`claimed_at`, 60 minutes, taken atomically): one `(agent, level)` is checked
by one holder at a time, a second caller finds it held and moves on (the durable count makes it due
again later), and different agents' checks run side by side in their jobs' tasks
([[base/agents/history/hierarchy/docs/chunks.ava.okf.md|concurrency]]). A check's write sets the
baseline to the open count read in its own transaction, so a leaf the agent's next job landed
meanwhile is counted. Nothing runs when `AVA_UNDERSTANDING_ENABLED` is off (the loop idles).

## The call and its rules

One conversation per check: the open nodes by id, with time (cluster clock) and text,
oldest first. The reply is **plain text**, like a chunk call's: one
`<group first="ID" last="ID">summary</group>` per group, no tool, no structured output, no
provider-specific path (a structured or tool-use reply made the model draft and re-draft whole
JSON in its thinking: roughly 2.7 times the cost of plain text on the same input). The prompt
states the purpose (group consecutive summaries about the same matter and summarize each group
one level higher; the reader can always open the members), the rules (no gaps; leave the newest
summary out of every group, and the last few if their matter is still going on; no groups at all
is a fine answer) and the format; nothing about language, length, audience or what matters. Code
parses the groups and enforces:

- groups start at the oldest open node and follow each other without gaps;
- the reply is well formed (every `<group` tag is a complete element; a missing `</group>` would swallow the next group, so it is refused and corrected);
- every id is a listed node and `last` is not before `first`;
- the newest open node is never grouped — the last group stays open;
- group sizes are the model's, with one exception: a group of one summary at the END of a reply stays open (no error, no correction, the prompt does not say so) and joins the next check with newer nodes; one at the start or in the middle is closed like any other, because the open nodes must stay one contiguous run from the oldest (a parent over a node that stayed open would overlap the parent that later covers it); a reply of only trailing one-node groups under `must_close` closes nothing and is refused like an empty one (corrected, then the check fails and the level waits for more nodes); no maximum;
- a reply with no group declines (the topic is still going), except when the open nodes number at
  least three times its level's threshold (set by the consumer as `must_close`:
  the prompt says so and code refuses a reply with no group) — the only brake on a level whose model
  keeps declining.

A reply that breaks a rule goes back in the same conversation with the exact problem, up to
`AVA_UNDERSTANDING_GROUP_CORRECTIONS` (default 2) more calls; past that the check
fails: the `understanding_group_failed` event, no node, the level looked at again after
that many more open nodes (the accepted gap). No fuzzy matching.

## Result and record

Each closed group is one node at level+1: span from its first child's start to its
last child's end, times the children's extremes, `children_count`, the children's
`parent_id` pointing at it — written with the cursor move in one transaction.
`AVA_UNDERSTANDING_GROUP_REASONING` sets these calls' reasoning: empty (default) leaves the
model's own tier untouched; `off` disables thinking where the provider allows it; any other value
is passed as the effort. `AVA_UNDERSTANDING_GROUP_MODEL` picks the model (empty = the agent's own via
`agent_model_target`; a grouping request has no agent prefix, so cache parity does not
bind it). Every provider call is a row of `understanding_group_calls`: the request of
that round (prompt or correction), the reply as returned, usage, timing, why the reply
was refused, the error of a failed call; rounds of one check share `check_key`. The
nodes' generation cost is not yet attached to upper nodes in serving (leaves only).
