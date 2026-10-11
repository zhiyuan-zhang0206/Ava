---
type: doc
title: "Manual compact recovery and authority"
description: Single attempt and finite failure, without automatic cold continuation repair.
---

# Interruption and authority

GET `/api/keyed/v1/agents/{id}/compact-commands/{command}` exposes the original
acceptance, outcome, reason, attempt, provider, execution, result availability,
checkpoint and continuation-release receipt. Historical recovery fields remain
readable, but no new automatic cold recovery populates them.

A fresh accepted compact has one provider attempt. If generation, result saving,
checkpoint application or acknowledgement fails, the original call stops.
The host attempts one diagnostic UNCERTAIN write; failure to save that diagnostic
is logged too. An already admitted attempt is never generated or applied again
on a later wake. Its pending continuation remains held for inspection. A later
host does not materialize the summary, certify closure or automatically settle an
associated guarded restart. A retained result exempts nothing: the later wake still
runs native cancel recovery, which marks an unresolved cancel UNCERTAIN, before the held
compact stops ordinary work. Normal original compact and restart still complete
when all their writes succeed.

A construction refusal before attempt admission releases REJECTED with
`single_attempt_unavailable` or `provider_unavailable`. This allows ordinary
new input and differs from an unknown admitted execution.

Runner authority retains INSERT on source observations and UPDATE on execution,
result and closure. Gateway admission owns command INSERT; receipt deletion,
observation rewrite and agents_meta INSERT stay outside runner authority.
Older running binaries and deployment compatibility require separate operator
handling; installed routes alone do not update runtime behavior.
