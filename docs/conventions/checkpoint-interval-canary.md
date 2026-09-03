# N-step checkpoint canary protocol (AVA_CHECKPOINT_INTERVAL)

Operational recovery-validation and rollback protocol for the default
`AVA_CHECKPOINT_INTERVAL=4`, and for evaluating a larger interval one agent
subset at a time. It formalizes the notes in
[`conventions/runbook.md`](../../conventions/runbook.md) (checkpoint section)
and the task #1551 write-amplification evaluation
(~39 checkpoints/min, ~169KB/step, ~340MB/h/agent before the throttle;
`N=4` cuts checkpoint write volume by ~75% before terminal flushes).

## When to use this protocol

`AVA_CHECKPOINT_INTERVAL` is a per-agent flag (`per_agent=True`,
`restart_required=agent`), default `4`. Set it to `1` for an agent that needs
every-super-step persistence; an interval above `4` trades more recovery
granularity for write volume and must go through this protocol.

## What N>1 changes (the risk being canaried)

- **Write path**: LangGraph `aput`/`aput_writes` are throttled to every Nth
  super-step; `input`/`fork` and the end-of-turn terminal state are never
  throttled (termination must land on disk).
- **Crash recovery**: replays up to `N-1` super-steps — re-spending LLM tokens,
  possibly replaying tool side effects. Inbound messages are re-delivered by
  claimed/pending reconciliation, so a crash mid-turn does not lose an inbound,
  it replays work.
- **Checkpoint parent chain**: one node per N steps — time-travel granularity
  coarsens (QA/405 are aware; a user-facing "step back" shows N-step jumps).
- **Interaction with disk protection**: independent of and stackable with the
  checkpoint reaper (Rule A/B) and blob VACUUM.

## Canary procedure (subset by subset)

1. **Pick the canary subset.** Start with 2-3 low-traffic, non-critical
   agents (never the gateway, never a resident role that holds external
   state — e.g. the Memory Arbiter or the health steward). Each canary gets a
   per-agent overlay for the proposed interval above `4` (config overlay wins
   over cluster `.env`; do NOT set the cluster-wide `.env` value first).
2. **Restart the canaries** (`restart_required=agent`):
   `ava agents restart <id>` (or the frontend agent settings page).
3. **Verify write reduction** (the thing the flag exists for): watch the
   checkpoint write rate / `checkpoint_blobs` growth over one full day against
   the pre-canary baseline. Compare the result with N=4 before accepting a
   larger recovery window.
4. **Forced kill → resurrect recovery check** (the thing that could regress):
   for each canary, `ava agents kill <id>` mid-turn (hard stop — `terminate` is
   graceful and lets the agent finish its turn), then `ava agents resurrect
   <id>` / send it a message and confirm:
   - the agent comes back with its conversation state intact;
   - up to N-1 super-steps replay and the final state is consistent
     (no stuck "in flight" node, no duplicate-visible inbound loss — the
     claimed/pending reconcile re-delivers);
   - the checkpoint parent chain is walkable (no broken ancestors in the
     fleet view).
   Do this at the proposed interval before widening; if recovery is broken,
   roll back (below).
5. **Widen**: canary a second subset covering one resident role + one
   high-traffic agent, repeat steps 2-4. Only after both rounds are clean
   consider a cluster `.env` `AVA_CHECKPOINT_INTERVAL=<proposed interval>`
   override (still per-agent overridable).

## Rollback

- **Per-agent**: set `checkpoint_interval` to `1` and restart the agent. The
  next checkpoint writes at the strict cadence; a crash during the transition
  replays at most the current interval's N-1 steps — the same bound as normal
  N-step operation, no special recovery.
- **Cluster-wide**: set `AVA_CHECKPOINT_INTERVAL=1` in `.env` and restart the
  affected agents. There is no data migration — the flag only changes write
  cadence; existing checkpoints (sparser parent chains included) remain valid
  and readable at any interval.
- If the canary subset shows recovery regressions, roll the SUBSET back only;
  never carry a broken subset forward.

## Success criteria (before widening past the first subset)

- One full day at the proposed interval on the canaries with write-volume
  reduction observed against the N=4 baseline.
- Forced kill → resurrect passes for every canary (step 4).
- No new watchdog respawns, wedged-loop alerts, or lost-inbound reports on
  the canaries.
- Fleet-graph / history / time-travel reads over the canaries' checkpoints
  show no anomalies (coarser parent chains are expected, broken chains are not).
