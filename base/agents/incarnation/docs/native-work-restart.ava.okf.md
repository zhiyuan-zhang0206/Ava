---
type: doc
title: Guarded ACTIVE Native Work Restart
description: One original ACTIVE restart command with retained exact lifecycle execution facts.
tags: []
---

# Guarded ACTIVE Native Work Restart

The new keyed restart surface accepts only an observed eligible ACTIVE managed
`NativeWorkTarget`. It does not add idle, held or legacy NULL-resource targets.
The existing work protocol is target qualification; it cannot prove a restart
executor understands the new operation. No SDK/UI caller is enabled here.

## Acceptance owner

`base/agents/messages/native_restart.py` owns one immutable operation receipt in
`native_restart_commands`. Receipt lookup precedes mutable source, home-machine,
work and configuration reads. Its raw JSON comparison digest preserves boolean
and numeric representations; it compares requests and is not operation identity.
The operation key and original work UUID identify the operation.

Fresh acceptance locks key, metadata, original work, then lifecycle source. It
refuses unfinished competing lifecycle commands or another fresh key for that
work. The raw request, normalized overlay, original source ID and complete target
commit with the overlay update and pointer. The shared inbound insertion owner
records the existing audit in that transaction. The sync public acceptance
transport executes the same SQL as the existing async lifecycle owner.

The Gateway uses `/api/keyed/v1/agents/{agent_id}/restart-work`, forwarding the
same scoped key through `/api/agents/{agent_id}/restart-work-v1`. It requires the
versioned typed response and a matching actual durable receipt. Unsupported
responses fail closed without legacy fallback. The dedicated path bypasses only
the generic Ops response claim; it cannot free or replay arbitrary Ops claims.

## Execution and retained proof

The returned invocation selector captures the exact accepted original command
only after its original checkpoint flush and native cancel settlement. This
allows an already accepted restart to settle after a cancelled graph returns
with legacy restart flags clear. It never invokes the graph again. Generic
lifecycle flags retain their existing behavior and proof predicates.

The domain SQL trigger projects actual original lifecycle source facts under
exact command ID, kind, generation, owner, source and frozen overlay checks.
ACCEPTED is not APPLIED. APPLIED retains original ownership release; OBSERVED
retains real successor admission. Generic DONE, checkpoint flags, changed status
and an unverified JSON hint do not establish completion. Existing actual force,
resurrection or replacement supersession retains no-effect facts. Unknown source
transitions and a missing uncompleted source are UNCERTAIN.

The command status GET reads retained facts. Receipt-first replay returns the
original acceptance and never rewrites a later overlay, adopts a successor or
mints another command. Retained APPLIED/OBSERVED facts recover the original
completed invocation even after source cleanup. Receipt records have no TTL or
cleanup FK.

## Compatibility

Legacy routes and source producers remain unchanged. Strong activation requires
retiring/draining old Ops producers and native claim/admission consumers for the
target. Dedicated receipts cannot make an old binary honor the new operation.
An old executor, missing proof or unknown transition is not a completed restart.
This contribution authorizes no deployment.

Related: [[native-work-cancel.ava.okf.md]] and
[design](../../../../future/agent/guarded-active-restart.md).
