---
type: doc
title: Guarded native work cancellation
description: Exact work acceptance, committed halt proof and certified cold recovery.
---

# Guarded native work cancellation

The hosted runtime generation spans multiple turns. `native_graph_work.id` is
one actual graph invocation's UUID, retained through preparation, execution,
checkpoint flush and database recovery. Its immutable original tuple includes
agent, machine, generation, host owner and explicit native work protocol 1.
`agents_meta.native_work_id` is the current pointer; an agent-level pending
command also prevents pointer loss from admitting another work.

`base/agents/incarnation/native_work.py` owns work admission and transfer-chain
validation. `base/agents/messages/native_cancel.py` owns dedicated command
acceptance and completion. Neither uses the generic inbound queue: an unknown
non-chat inbound can otherwise be consumed before execution, and co-batched
chat can clear a halt. Metadata ownership locks precede work locks throughout.

## Guarded HTTP contract

GET `/api/keyed/v1/agents/{agent_id}/native-work` exposes only ACTIVE work with
an actual admitted managed resource set and live exact hosted owner whose
metadata is still running. A crashed turn settled to idling is not newly
eligible even if its historical work fact remains ACTIVE. Running status is
only a negative eligibility gate, never resource-closure evidence. Installed
code, a version column, PREPARING work, empty idle preparation, legacy NULL
resources and an active external impersonation lease do not qualify.

POST `/api/keyed/v1/agents/{agent_id}/cancel-work` requires a verified
`principal-v1` scope, `Idempotency-Key`, and every observed tuple field, including
integer protocol 1. Missing, boolean, floating-point and unknown protocols fail
at the raw boundary. Same-key replay reads its immutable original receipt before
mutable work or owner state; changed tuple conflicts. A different fresh key for
the same work conflicts rather than replacing the first command. Acceptance
returns a command identity, not evidence that execution stopped. Automatic ambiguous transport retries, fallback ingress and a client outbox
are not supported. UI and CLI consumers observe and submit exact targets; see
[[gateway/agents/docs/control-acceptance.ava.okf.md]].

## Original execution and settlement

The host binds the original work UUID through its existing invocation loop and
database recovery. The existing two-second interrupt watcher observes that
work's dedicated command. Claim checks it under the metadata lock before
claiming any next batch; a co-queued user chat remains pending. The original
returned or unwound continuation records an exact `NativeCancelMarker` and
halted state, flushes the original saver and reads committed channels with a
fresh source saver. Pending writes, reconstruction caches and historical markers
are insufficient proof. The host also requires actual continuation resources
to settle; an empty database request map alone cannot override a live scope.

Completion rechecks original command, exact receiver and resource closure under
the owner lock and compares the latest committed root checkpoint ID. APPLIED
records the original execution `checkpoint_id`. Lost graph results, flush
responses or ACK responses reuse the original work/result and command rather
than invoking the graph again. Accepted native cancel settles before ordinary
lifecycle or external handoff; original lifecycle receipts still own lifecycle
completion verdicts.

## Certified cold recovery

Resource admission alone produces transfer evidence, in the same transaction
as successful hosted admission. It retains exact original and successor tuple,
machine and process identities. The existing empty unfrozen predecessor rule
requires actual admission-captured host process death. The lifecycle rule
requires the existing actual predecessor receipt; a frozen set grants strong
proof only for its exact applied and observed terminate command. Unknown,
unobserved, wrong-owner or mismatched frozen evidence never certifies stop.
Every hop is validated continuously from the immutable original work to the
current consumer. A lease, status, elapsed time or arbitrary old receipt cannot
substitute for that chain.

An exact latest original marker can complete APPLIED under a certified receiver.
Without the original marker, a certified receiver first writes its own pause
projection (`halted=True`, inactive/idle, original work, no original cancel
marker), flushes it and cold-reads the committed pause. Latest-head and receiver
CAS then records RECOVERED_STOPPED with `recovery_checkpoint_id`; execution
`checkpoint_id` remains NULL. This proves predecessor stop and the consumer's
pause, not an original halt transition. The pause prevents a non-halted old
conversation from continuing without new input. New chat and an explicit
resurrection retain their existing resume behavior under the next work UUID.
Terminal replay never rewrites a later legitimate checkpoint.

A legal certified replacement cannot overlap this pause writer. The existing
serialized host pump shields and awaits the whole continuation, including
startup recovery and flush, before producing `quiescent=True` force observation.
Boot orphan recovery requires exclusive old-host retirement; actual process
exit makes the predecessor unable to resume. Resource and lifecycle proof
producers keep these contracts; there is no additional lease-based writer gate.
A concurrent force may invalidate the old receiver's final ACK, but its
observation and a successor's certificate wait for the original writer to drain.

Missing pointers, malformed/latest checkpoint proof, unresolved resources or
an uncertified receiver preserve UNCERTAIN and hold new strong-path work in
supported consumers. They
do not guess APPLIED or RECOVERED_STOPPED. Ordinary startup without a pending
strong command retains its existing behavior; an old unprotected work can be
abandoned without claiming execution or stop. Work and command records have
no TTL or foreign-key dependency on history. Retirement needs an explicit
future retention policy; this change does not rewrite old history or enable
legacy managed resources.

## Binary compatibility and activation

The dedicated command table prevents a legacy generic inbound claim from
consuming or falsely acknowledging a native cancel. It does not stop an old
binary from continuing its legacy graph/claim path. Old serializers may degrade
new typed checkpoint values to raw dictionaries rather than rejecting them, and
old admission does not append native transfer certificates. A supported receiver
without the complete stop chain stays UNCERTAIN; an unsupported old receiver is
not covered by that hold guarantee.

Operator activation requires retiring or draining every old native runtime,
claim and admission consumer for the target before strong callers are enabled.
The protocol does not promise safe old/new concurrent execution during rolling
replacement. The SDK offers explicit observed-work control through
[[ava/agents/docs/work-control.ava.okf.md]]; it does not replace legacy controls
or automatically submit commands. The same operator activation prerequisite
applies. UI and CLI now use that same observed-work protocol. MCP activation remains
separate; repository consumer changes do not deploy or activate a running fleet.

Owners: `services/agent_runner/agent_host/invocation/native_work.py`,
`agent/ownership/native_cancel.py`, `agent/ownership/hosted.py`, and
[[incarnation-resources.ava.okf.md|incarnation resource evidence]].
