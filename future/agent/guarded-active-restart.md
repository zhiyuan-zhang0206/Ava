# Guarded restart of one ACTIVE native work target

Scope: the complete ACTIVE restart operation in #4474, building on native
cancellation PR #4592. Idle preparation, managed-NULL runtimes, terminate,
force, resurrection, impersonation and client activation remain separate.
This is an execution-recoverable domain command, not an Ops response cache.

## Existing defect and owners

`gateway.agents.lifecycle.post_agent_restart` forwards an unkeyed request.
`gateway.agents.forward.enqueue_lifecycle` starts a new Ops dispatch on every
HTTP call; `ops.cluster.rpc` mints a new lifecycle UUID key for each dispatch.
`ops.lifecycle._restart_blocking` updates the overlay and inserts another
restart command. A lost HTTP response followed by a repeat can restart the
successor and restore the old overlay.

The execution owner already exists. `base.agents.incarnation.lifecycle_acceptance`
owns command acceptance/target binding; `agent.ownership.hosted.apply_hosted_lifecycle`
requires the original exact command, actual settled resources and original
owner before APPLIED; `agent.ownership.lifecycle_intent.observe_hosted_admission`
records restart OBSERVED in successor admission. These proof predicates remain
unchanged. The new domain binds the HTTP operation to that original command.

## Interfaces and immutable acceptance

- POST `/api/keyed/v1/agents/{agent_id}/restart-work`: verified principal-v1
  operation key, positive BIGINT path, explicit `NativeWorkTarget` and restart
  options (`source`, raw `config_overlay`). Every observed target field and
  protocol is required. No SDK/UI activation or legacy fallback.
- Internal fixed `/api/agents/{agent_id}/restart-work-v1` lifecycle path:
  explicit scoped operation key and the same original request. Old Ops servers
  reject this path. Native work protocol 1 is target qualification, not evidence
  that the restart executor supports this request protocol.
- GET `/api/keyed/v1/agents/{agent_id}/restart-commands/{command_id}`:
  original receipt and actual progress, separate from immutable acceptance.
  ACCEPTED is not an executed restart; APPLIED is original release; OBSERVED is
  real successor admission. Proof gaps are UNCERTAIN, never cached success.

A dedicated `native_restart_commands` receipt retains operation key, exact
original target, raw request, frozen normalized overlay, original inbound ID,
immutable acceptance, and original execution/supersession facts. No cleanup FK
or TTL. The receipt lookup precedes mutable source/owner/configuration reads.
Changed request conflicts; replay returns the original acceptance even after
owner, work, source command or configuration changes.

Fresh acceptance locks the operation identity, metadata, then original work.
It requires the same eligible ACTIVE managed native target and refuses an
already unfinished lifecycle command or a concurrent competing guarded restart.
It inserts one restart inbound, applies the frozen overlay, and invokes a sync
transport of the existing acceptance SQL in the same transaction. There is no
parallel target-binding writer. The command is already bound to the original
incarnation at acceptance; a successor must never adopt it as new intent.
The receipt, source command, overlay and exact pointer commit atomically.

The generic Ops response claim is bypassed only for the new fixed domain path:
its dedicated receipt owns same-key recovery. An incomplete generic claim does
not block recovery of a committed domain acceptance, and cannot be stolen to
reexecute arbitrary Ops. Authenticated forwarding preserves the scoped key and
raw request; the key is not regenerated per HTTP attempt.

## Execution facts and recovery

The current host applies and observes the original command using its existing
proof predicates. Completion must also persist the matching facts in the
retained domain receipt in that source transaction. A bounded domain trigger on
restart inbound updates is the bounded projection: it copies only original
command ID plus exact target generation/owner and actual source facts. It does
not execute operations or infer completion from metadata status, a checkpoint
boolean, elapsed time, an installed version or a changed owner. Source deletion
cannot erase an already retained fact; absent proof remains UNCERTAIN.

An unapplied old-target command is settled by the existing replacement,
resurrection or force supersession owner rather than executed against the new
owner. Its original no-effect outcome is retained, not relabeled APPLIED.
Existing original-return database recovery from #4576 continues to recover the
original lifecycle result, preserving checkpoint flush and co-queued chat.
Same-key acceptance recovery only re-announces the original unfinished command;
it neither rewrites the overlay nor mints a command/work UUID.

## Required evidence

Real Postgres and hosted graph/owner tests cover: simultaneous same/different
keys; changed raw body/target; acceptance commit response loss; original graph
return, checkpoint flush, apply commit and observed commit response loss;
daemon/Gateway reconstruction; original owner exit before apply; actual
successor admission; A accepted then B current then old A replay; source cleanup;
withdrawn model normalization once; a later overlay never overwritten by replay;
co-queued chat preserved; legacy route behavior; missing protocol/invalid path;
and old versioned-path rejection with no fallback. The complete candidate must
show one original lifecycle effect and no second graph invocation across lost
responses, not just one accepted command.

Activation requires retiring/draining old native claim/admission consumers and
old Ops producers. Dedicated receipts cannot make an old binary honor a new
operation protocol. This contribution authorizes no deployment.
