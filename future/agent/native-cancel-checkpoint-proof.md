# Native cancel: work identity and checkpoint evidence

Proposed implementation for [#4474](https://github.com/zhiyuan-zhang0206/Ava/issues/4474).
This plan is not an implemented capability or deployment authorization. Its base
is third integration `fa0847e987bdf95ba95ff7b35a1fa458e904f32d`.

## Current owners and gaps

- `AgentHost._invoke_prepared_graph` prepares/settles before invoking the shared
  graph; `_invoke_until_done` and `PendingWorkResult` retain the returned result
  and trace through database retries. They do not persist a work UUID.
- `claim_node` owns the invocation boundary (`turn_active`/`turn_idle`). The
  generic `claim_inbound_batch` marks non-chat rows DONE before dispatch;
  legacy cancel plus co-batched chat can clear the halt. Protected commands
  must never enter that table or reuse legacy cancel payloads.
- `subscribe_interrupt` has an existing two-second watcher. It aborts owned
  model/exec operations but does not claim/apply commands. `InterruptEvent`
  currently retains only the first interrupt reason.
- `admit_resources_async` and `_dead_predecessor_evidence` verify resource and
  exact-process/lifecycle closure in the admission transaction. They currently
  overwrite resource ownership without retaining a typed transfer certificate.
- `flush_checkpoint` drains the existing optional N-step tail; failure preserves
  the tail. Saver `aget_tuple` reads a persisted tuple. Graph state and pending
  writes are not cold committed-channel evidence.

## Identity, storage and admission

Introduce one narrowly owned native work record per actual graph invocation,
with immutable `(work UUID, agent, machine, runtime generation, actual host
owner, native-work protocol)`. This protocol is independent of the existing
runtime identity-envelope protocol. Reuse the existing turn context to carry
the work identity to graph nodes and interrupt subscribers; mirror it in a
dedicated checkpoint state channel, outside provider messages and prompts.

The host will create PREPARING work under the metadata lock after preparation
and before invoking. Claim will activate that same UUID immediately before
routing real native work, including continuing an existing conversation; an
empty idle invocation remains ineligible. Database retries preserve the same
record/result. A pending prior work or command prevents minting another UUID or
claiming another chat. Managed resource/actual-owner proof is required to
advertise protocol support; legacy NULL resource state remains unsupported
without changing its legacy graph behavior.

Use dedicated domain work and cancel-command storage, plus the metadata work
pointer. The command row owns its immutable principal/path/key/request and
original acceptance together with its mutable execution evidence. Unique work
identity permits only the first fresh cancel for that work; another key returns
409 even after the original command settles (provisional first-slice policy).
Receipts have no agent/work cleanup foreign key or expiry. Work/certificate and
unsettled command records are excluded from unrelated inbound cleanup; explicit
retention remains a later policy.

A versioned observation GET will expose the actual eligible tuple; the guarded
cancel POST requires that complete tuple, `Idempotency-Key` and exact
`principal-v1`. Reauthenticate, validate raw input, lock scoped key, look up
original receipt before mutable state, then lock metadata/work for fresh
acceptance. Changed immutable target under one key is 409. Fresh idle,
terminated, PREPARING, stale owner/work, unsupported protocol and active external
impersonation refuse before effects. No SDK/UI activation, automatic retry or
legacy fallback. Old consumers ignore the dedicated domain and cannot falsely
acknowledge its command; an old successor also fails the original-owner tuple.

## Execution and original-result settlement

Extend the existing watcher to observe a command only for its bound exact work
and retain its command/work attribution. Its event remains an abort hint, not
an ACK. At claim, check the dedicated command before claiming generic input or
external handoff. Return a halt/END transition containing exact command UUID,
work UUID and original owner tuple in the checkpoint marker, without claiming
co-batched chat. A command accepted after graph return but before settlement is
handled by the same single-flight database settlement boundary: update only
that original work's halt marker, never invoke or claim another work to apply it.

Flush the saver, then read a fresh persisted tuple and require both matching
marker and halted state in committed `channel_values`. Ignore graph return,
pending writes and SSE. Retain the checkpoint ID as evidence. After the real
continuation and managed resources settle, ACK with metadata/work/command CAS;
only then close the work and allow another invocation. ACK response loss reads
the original retained command, never reruns the graph or retargets a cancel.
No later control may overwrite an unacknowledged marker. Fatal failure and
force/lifecycle paths must pass the same work-settlement boundary.

## Cold recovery proof

Extend the existing resource admission owner to return a typed proof using its
actual rule, not a second evaluator. Before replacing an unsettled work's owner,
the same metadata-lock admission transaction must retain a certificate binding
that original work/agent/machine/generation/owner to the admitted successor.
Certification requires the actual empty, unfrozen managed resource set and
either exact predecessor host-process exit or the original applied lifecycle
receipt accepted by `resource_admission`; successful successor admission and
the certificate commit together. Birth, NULL legacy adoption, lease expiry,
mutable status and a different machine/tuple are not substitutes.

Before normal startup repair, activation or new chat claims, recover the old
work: exact committed halt marker permits ACK of the original command; absent
marker plus the matching certified transfer permits DB-only RECOVERED_STOPPED.
The latter proves the original work stopped, not that its cancel transition ran.
Missing/ambiguous evidence stays UNCERTAIN and holds new native work. No cold
graph replay, marker fabrication, resource TTL stealing or relaxed owner CAS.
Certificates and original receipts remain usable after further successor turns.

## Decisions to confirm before code

1. Only routed native work is ACTIVE; PREPARING/empty idle observations refuse.
   This avoids making idle cancel an instruction to stop a later wake.
2. An accepted cancel settles before ordinary pending lifecycle/handoff dispatch
   while its original owner remains live. Independent force termination may
   supersede it only with the certified-stop proof above. Legacy routes retain
   their existing behavior; no UI execution claim is added.
3. A proof gap holds future native work as UNCERTAIN. The operator recovery
   policy is not an automatic restart, replay or inferred success. Managed
   resource NULL targets remain outside the strong capability.

## Required evidence and implementation sequence

First implement immutable work/command identity, versioned admission and typed
resource-transfer certification together with meaningful real Postgres tests.
Then connect the existing graph watcher/claim halt, flushed cold evidence, ACK
and hosted original-result/cold-start recovery in the same PR before exposing
the capability. An admission-only endpoint is not the completed feature.

Tests must cover two works in one generation; stale work and owner; one-key
duplicate versus changed tuple and another key; PREPARING/idle/unsupported and
impersonation separation; cancel with queued chat; real model/exec interrupt
cleanup; acceptance/marker/flush/ACK before/after-commit loss and buffered saver
tails; checkpoint success with lost ACK; no extra graph invocation after DB
recovery; cold exact-dead-empty and lifecycle-certified transfer; transfer
rollback; live/frozen/nonempty/NULL/wrong-work proof rejection; missing marker
RECOVERED_STOPPED versus missing proof UNCERTAIN; stale ACK and old consumer
generic claim/interrupt isolation; legacy cancel and lifecycle behavior.

Source owners will be additive `base/agents/messages` domain primitives,
`base/agents/incarnation/resource_admission`, existing hosted admission and
invocation/settlement helpers, checkpoint state/claim/interrupt, guarded Gateway
contracts and derived schema/types. No new worker, generic queue or polling
framework. Migration/schema convergence and full real affected consumers are
required. Compact preparation remains a separate future slice.
