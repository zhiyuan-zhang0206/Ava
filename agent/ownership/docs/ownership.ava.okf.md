---
type: doc
title: Agent Runtime Ownership
description: Hosted incarnation leases fence admission, settlement and resource cleanup.
tags: []
---

# Agent Runtime Ownership

`agents_meta.runtime_generation`, `runtime_owner`, `runtime_kind` and
`lease_expires_at` identify the admitted hosted runtime. The generation scopes an
agent incarnation; the owner identifies the host boot. A delayed task cannot
renew or settle another incarnation.

Ownership spans normal turns, idle and model-cache eviction. Idle has no active
task even when its host retains ownership. `renew_hosted_owner()` refreshes only
the current host owner's rows; `release_hosted_owner()` and
`settle_hosted_runtime()` match the original incarnation. Restart releases it for
a new generation. A fresh foreign owner blocks admission and stale-row recovery.
A resurrection that closes out a closed retained incarnation records the
released generation and owner on its own inbound, and the successor consumes
that release record for the same live host process; every unmet closure is a
recorded refusal (`last_admission_outcome` plus a `host_admission_refused`
event), not a crash. See the shipped follow-up in
[the unowned-termination decision](../../../docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md).
A stopping host's release is bounded (`services/agent_runner/agent_host/host.py`,
`_RELEASE_OWNER_TIMEOUT_S`) so the whole stop fits ava-root's TERM window; with
the database unreachable it fails and the leases expire by TTL.

Lease TTL and renewal ordering live in `base/deploy/progress_timeout.py` and
`base/deploy/timing.py`. A lease is runtime ownership evidence, not an agent identity
count: the status page counts non-terminated local identities, including idle
and maintenance-paused agents. Heartbeat selects eligible idle identities
without requiring an independently resident agent process.

## Entry points

- `agent/ownership/hosted.py` — admission, renewal, settlement and release
- `agent/ownership/hosted.py:settle_stale_running_rows` — host recovery
- `agent/ownership/hosted_claim.py` — the admission UPDATE that claims the runtime row
- `agent/ownership/corpse_reap.py` — crash-dead row termination + recovery wake; the host supplies a live wake-enabled reader, called at each original per-row transaction gate
- `agent/ownership/inbound.py` — the owner lock every inbound queue mutation takes
- `agent/ownership/lifecycle_intent.py` — the durable lifecycle command pointer
- `services/agent_runner/agent_host/daemon.py` — owner health beat
- `base/native_process/runtime_incarnation.py` — context-bound execution identity

Related: [[../../startup/docs/admission.ava.okf.md]] and [[../../docs/lifecycle.ava.okf.md]].

A runtime generation is not a graph work identity. The native work owner retains
one invocation UUID across database recovery, dedicated cancel acceptance and
committed halt/certified-stop proof. Hosted admission appends an actual resource
transfer in its successful metadata transaction; later startup cannot manufacture
that certificate from mutable status. See
[[../../../base/agents/incarnation/docs/native-work-cancel.ava.okf.md|guarded native work cancellation]].

Guarded ACTIVE restart retains the exact original lifecycle command and its
source-transaction completion facts. The completed-invocation selector runs
after native cancellation settles and preserves later queued chat. See
[[../../../base/agents/incarnation/docs/native-work-restart.ava.okf.md]].

Requested PTY cleanup runs after lifecycle commit. Graceful apply and force
acceptance capture the next monotonic shell ID under the agent row lock; the
original host's validated quiescent force settlement captures one fresh cutoff.
Each cleanup attempts only listed IDs below its cutoff, excluding page sessions,
through the existing backend identity checks. It never holds the lifecycle lock
across PTY calls or chases later allocations. Errors stay visible without rolling
back termination; crash leftovers and late sessions require operator cleanup.
Graceful post-commit notification, browser-page release and requested shell cleanup
are independently attempted. A cancellation during browser release cannot skip shell
cleanup; the first failure is raised after the attempts, with later failures attached
as notes. Ordinary browser-release errors remain best-effort warnings.
Execution-child resource closure and admission fences remain separate. See
[the cleanup decision](../../../docs/decisions/runtime/processes/2026-10-10-termination-pty-cleanup-after-commit.md).

## Process fixture cleanup

`test_hosted_dead_owner_admission.py` and `test_hosted_ownership.py` use SIGKILL
for their owned look-alike host processes. A child can inherit ignored SIGTERM
from a shell session, so `terminate()` alone cannot guarantee fixture cleanup.
