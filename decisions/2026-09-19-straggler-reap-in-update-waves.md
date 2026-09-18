# Update waves reap un-landed stragglers instead of aborting on them

## Context

An update wave must move every native agent onto the new code, and a cohort
member mid-`execute_code` reaches its durable restart boundary only when its
current exec ends (up to the 300s exec ceiling). The 2026-08-26 ruling already
priced this: an interrupted agent task is acceptable, a fast cluster unblock is
worth more. The 2026-09-07 hosted refactor (#1924) then removed the per-agent
process and with it the 8/26 CAS→respawn chain, leaving a "never kill" drain:
Phase A waits `update_quiesce_timeout_seconds` (300s default), aborts on the
stragglers, and the wave retries — which is exactly the cost the 8/26/9-19
rulings rejected. At 50 concurrent agents one straggler could stall the whole
wave for minutes at a time.

## Decision

Per user ruling 2026-09-19 (405's design, task #4016): re-ground the reap on
the hosted world's own machinery.

1. **Window W** — new cluster-pinned `update_straggler_reap_seconds` (default
   15, 0 disables). Counted per cohort member from ITS restart command's
   issuance (`inbound_messages.created_at`, DB clock). Only update-family
   drains (`pause_local_cluster`, the update quiesce) reap; interactive
   pause/stop/restart keep the never-kill contract.
2. **Kill = a CAS mark, not a signal.** The drain CAS-marks the row
   `running → 'restarting'` and `agent.db.has_pending_interrupt` treats that
   mark (with its still-un-applied maintenance restart) as an in-flight abort
   signal: the running exec/LLM node truncates within its existing poll
   cadence and the exec subsystem settles its child tree. No new kill HTTP
   surface: a hosted turn is not a process, and the status predicates of the
   mark fence every old-incarnation write path (claim acceptance, lifecycle
   apply, settle, corpse stamp) without chasing races.
3. **Honest receipts.** The member is released through a new `reaped` hold
   receipt (`MaintenanceHold.reaped`); `verify_drained` certifies it by the
   reap state (row still 'restarting', command never applied/observed) and
   never fabricates `applied_at`/`observed_at` or a flush receipt. A refused
   reap aborts the drain as before.
4. **Successor settle.** `shared/straggler_reap.settle_stranded_reaps[_async]`
   runs at the agent-host boot and at the local unpause/start boundary (the
   compensating resume of an aborted wave runs while the host stayed up): it
   closes the never-applied command as `done` with
   `lifecycle_result={"outcome": "reaped"}`, restores the row to `idling` with
   ownership/lease released, and wakes the agent once so its first admission's
   existing reconcile re-delivers the claimed work the reap cut short.
5. **Exclusions.** Rows under an external takeover/lease (requested, accepted,
   or live active) are never reaped — the graceful path stays; W is decoupled
   from `exec_timeout_seconds`; maintenance-authored command semantics are
   untouched.

## Alternatives rejected

- *Keep "never kill" (the 9/7 status).* Overturned by the 9/19 ruling: one
  straggler aborting a 50-agent wave costs every other agent the retry.
- *Introduce a `/cancel-turn`-style remote kill or revive the task-cancel
  path.* `Task.cancel()` cannot stop the shielded turn work, and the durable
  interrupt already exists — the mark extends it rather than adding a second
  kill mechanism beside it.
- *Settle the row at drain time (release ownership immediately).* A hosted
  turn may still be unwinding: clearing the incarnation while the dying turn
  can still take a claim pass would crash its lifecycle acceptance. Settling
  at the boot/resume boundary happens after the predecessor is provably gone
  (or the wave is over), from the same authority that releases the hold.
- *Leave the claimed restart for the ordinary checkpoint reconcile.* The
  reconcile only re-delivers ordinary claimed rows; a claimed lifecycle row
  would dangle pointing at a dead incarnation and block the next wave's
  prepare.
- *Fold W into the quiesce timeout or exec_timeout.* Already rejected on
  8/26: the exec timeout is an agent-runtime safety bound, and the quiesce
  window keeps its own meaning (the wave's overall drain budget).

## Consequences

- A wave drains in ~W instead of aborting at the 300s bound when stragglers
  exist; the reaped agents lose their in-flight exec (their checkpoint and the
  claimed work re-deliver on new code — at-least-once, side effects may repeat,
  accepted by the 9/19 ruling and inventoried in the PR).
- `reaped` becomes a visible drain outcome (hold journal, telemetry
  `update_straggler_reaped`, `update_straggler_reap_settled` at settlement);
  an operator sees exactly who was truncated instead of a bare agent list.
- A crashed wave without a resume leaves the marks until the next boot or
  resume; a subsequent wave then refuses those rows once (existing
  blocking-residue behavior), and its own failure path's resume settles them —
  self-healing on the retry.
