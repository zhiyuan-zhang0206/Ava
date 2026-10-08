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
