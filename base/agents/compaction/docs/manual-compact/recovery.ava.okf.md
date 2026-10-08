---
type: doc
title: "Manual compact recovery and authority"
description: "Unknown result closure, retained diagnostics and exact runner authority."
---

# Recovery and authority

GET `/api/keyed/v1/agents/{id}/compact-commands/{command}` exposes original
acceptance, outcome, safe reason, attempt/provider/execution, result availability,
application checkpoint, a separate recovery checkpoint and native continuation release. It does not expose
the summary as a current conversation snapshot.

Missing result after a claimed attempt stays UNCERTAIN. Only actual executor
unwind/resource closure, or an exact public certified receiver chain, may write
a consumer pause, flush it and prove the latest cold no-application head before
releasing the pending pointer. This is diagnostic recovery, not successful
compaction: execution `checkpoint_id` stays null. A lease timeout, arbitrary new
owner or status change cannot certify an unknown executor stopped. Without that
proof ordinary inputs remain pending. An existing application marker cannot be
reclassified as an unknown-generation pause.

The authority owner grants runners INSERT on immutable source observations
and UPDATE on command execution/result/closure. Command INSERT is verified
Gateway admission; observation rewrite, receipt DELETE and agents_meta INSERT
remain outside runner authority. Real runner credentials exercise the entire
host chain and checkpoint/native-work closure.

Running generations must support this protocol before operators use it. Older
binaries may still execute legacy commands or raw checkpoint mutations and do
not gain guarded support merely from installed schema/routes. Deployment,
retirement of older generations and operational reconciliation are separate
operator actions; this contribution performs none of them.

A construction refusal before attempt admission releases a REJECTED receipt
with `single_attempt_unavailable` (or `provider_unavailable`), without execution
or provider evidence. This differs from a claimed attempt's unknown result.

The serialized driver and actual resource tail hand an accepted guarded restart
to the original immutable compact execution before ordinary work may mint a
new UUID. SQL response loss reads original APPLIED/OBSERVED proof before database
repair. Actual generation failure and native cancellation use the same handoff
after diagnostic/native closure. A dead executor's settled work is not transferred
as ACTIVE execution: its admitted successor uses the existing lifecycle
`target_replaced` settlement, retaining SUPERSEDED with no application timestamp.
The original restart owner also certifies `resurrect` and `force_terminate`
no-effect receipts. These retain their original reason across source cleanup.
That no-effect receipt permits new input; it never restarts the successor or
reapplies the original overlay. Unknown restart proof retains the gate.
