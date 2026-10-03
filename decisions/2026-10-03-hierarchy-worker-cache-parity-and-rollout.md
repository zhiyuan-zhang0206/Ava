# The hierarchy worker rides the agent's own request head, rolls out by allowlist, and defers first builds

## Context

The understanding tree reaches no agent prompt — its only reader is the run-timeline HTTP
endpoint — so enabling the worker cannot move an agent's prompt-cache hit rate. What it can
change is the worker's own cost: each node's generation request is agent-shaped, carrying the
agent's leading messages so the provider serves them from its cache. Three properties of the
shipped (dark) worker made that unsafe to switch on:

- the prefix was `msgs[:cut]` over the stitched full history, which concatenates every
  compaction segment, so past the first segment it was bytes the agent never sent and it
  grew with every segment;
- the model followed the overlay only (`llm_model` is birth-frozen, so a birth stamp
  was ignored) and the reasoning effort was overridden, both parts of what the provider
  matches its cache against;
- enabling it queues every agent's one-time full-window first build at once (more than
  three times the 24h anomaly breaker), which would trip the breaker and wait for a person.

## Decision

1. A node's request prefix is its own compaction segment's SystemMessage plus that segment's
   messages up to the node (`base/agents/history/hierarchy/prefix.py`), never the stitched
   history. A prefix that with its material would exceed 0.8 of the model's window, or a
   segment with no head SystemMessage, sends the material-only request. Rejected: truncating
   the stitched prefix to a token budget (still not the agent's bytes, so no cache hit).
2. The model is the agent's own: overlay over birth stamp over the live default, then the
   withdrawal fallback, built with no effort override. Rejected: keeping the cheaper
   summarizer effort — a reasoning-parameter mismatch costs the cache hit the shape exists for.
3. `AVA_HIERARCHY_WORKER_AGENTS` (empty = everyone) is a rollout allowlist beside the
   cluster-wide master switch, so one or two agents can be canaries. Rejected: a per-agent
   overlay switch — the worker is a gateway-side loop that reads cluster config, not a turn.
4. First builds are paced by their own 24h budget by deferral (the claim parks them, they
   resume as the window rolls), and the anomaly breaker no longer counts them. Rejected: one
   shared budget that trips — the one-time wave is expected load, not an anomaly, and a trip
   needs a manual reset.
