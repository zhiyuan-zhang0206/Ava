# The understanding tree is grouped by the model in cached chunk calls, not cut by a token-budget cascade

Decision (2026-10-07): an agent's history is described as it grows, by calls on the agent's own
request prefix, and the model decides where the groups of message units begin and end. This
replaces the compact-driven hierarchy worker (`hierarchy_jobs`, a per-minute schedule, the seal /
kappa cut, a regeneration breaker). Goal: a faithful, drillable tree for audit; compaction serves
the agent going on working and may forget, so the two do not shape each other.

What is decided:
- **Layer 0 is code.** Reasoning + tool call + results is one unit, agent text and each inbound
  message are units of their own, framework notes are units (`units.py`). No model.
- **Level 1 is one cached call per chunk.** When the provider-reported input tokens grow by a
  threshold past the previous cut, or a compaction closes the segment, the agent host's loop sends
  the agent's own prefix plus one instruction: a numbered catalog of the stretch's units. The reply
  names each group by its first and last catalog number and summarizes it; code looks the numbers up
  and requires the groups to tile the catalog. A topic that crosses a chunk end is two nodes there.
- **The levels above are plain-text grouping calls without the agent prefix**, due when a level's
  open nodes grow by a threshold that falls per level (60, 20, 7, 6, ...); the newest node and a
  trailing one-node group stay open, and a summary is shorter than the summaries it covers together.
- **Only "what happened" is a model text.** Costs and structure are code, summed up the tree.
- **No concurrency cap**: due jobs of different agents run at once, one agent's in order; rate
  limits are the provider's 429 and the shared backoff (five retries per call).
- **Every provider call is stored whole** (`understanding_chunk_calls`, `understanding_group_calls`);
  a chunk that fails for good stays undescribed and is reported, never filled in.
- **The old system is deleted**, not kept behind a switch; its tables are dropped by a later
  migration (expand-contract) and its old nodes are removed now.

Rejected, and why:
- *Folding units in batches of kappa with an append-only seal cascade and a per-node token budget*
  (the old worker): the cuts are mechanical and fall mid-topic, every level needs its own budget
  and compression pass, and the machinery (a job queue, scan cursor, breaker, tail seal, reuse
  cache) was larger than the question it answered.
- *A cache-warmth judgement (TTL) or dropping stale jobs*: a backlog is a queue-depth alert's
  concern; the design sends the request as soon as the threshold is crossed, while the cache is hot.
  *Terminate as a trigger*: agents terminate each other, so the prefix is not necessarily warm.
- *Open groups carried from one chunk to the next* (and quoting the group's first words to name a
  boundary): the carry cost the model long deliberation about what was still going on, confused it
  with the agent's own compaction reminders, and two ways of naming a unit (quote, then number)
  failed silently; a numbered catalog with both ends named is looked up and checked, and the upper
  levels join what a chunk end split.
- *Group-size rules* (5 to 30 units, 3 to 15 nodes): the model's cut by meaning was better than a
  bound, and a bound only moved the failure to its edge.
- *Structured output or tool use for the upper levels*: about 2.7 times the thinking cost of plain
  text on the same input, with no better groups.
- *Slots, a limiter and a thread pool sized to them*: one more thing to size; the provider already
  answers load with a 429, and a job holds a thread only while it runs.
- *Backfilling history, a daily budget, per-agent rollout allowlists*: not needed to answer the
  question; the switch (`AVA_UNDERSTANDING_ENABLED`) ships off and is turned on for a few agents first.

Measured on a real agent (agent 9, 11 compaction segments, 610 level-1 nodes): about 8% of the
agent's own spend at the chosen ratio, within the 10% ceiling; the top level lags the history end
by about a day (accepted: recent history is read at the lower levels). 99 of 102 factual claims
of a sampled segment were supported by their source.
