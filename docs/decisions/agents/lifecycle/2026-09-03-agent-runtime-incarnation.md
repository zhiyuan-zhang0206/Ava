# One runtime incarnation fact on agents_meta

## Decision

An admitted runtime carries a UUID generation, a process/hosted kind, and a
boot-instance owner UUID. These are assigned in the same transaction that
admits the runtime. Exit and lease renewal match the original token, never one
re-read from the current row. An unowned successor cannot be finalized by an
exiting predecessor. Missing tokens match only genuinely unknown legacy rows.

Reusing pid plus started_at avoids schema additions but does not identify a
hosted runtime, and a delayed callback must not infer ownership from the current
PID. A separate registry would duplicate agents_meta's admission transaction.
The additive columns replace agent-id-only ownership in the converted paths.

## Deliberately partial first slice

Process admission, exit, and lease renewal are fenced. Hosted admission remains
legacy/unknown and must acquire the same contract with owner-bound renewal next.
No existing row is backfilled. Protocol zero is unproven; this change advertises
no new envelope support in either mode. Existing hosted exit still works only
against unknown rows; it cannot finalize a newly owned process row.

## Mixed-version barrier

CAS cannot constrain an old binary issuing unconditional SQL. Before enabling
protocol v1, the deployment operator must verify every old lifecycle writer
(agents, hosted runners, restarters, and gateways) has stopped and each active
writer runs the fenced release. No global release SHA substitutes for each
target's admitted owner and fresh lease. Keep the producer disabled if this
barrier cannot be verified; offline hosts must upgrade before admission. The
automated barrier and hosted admission are required subsequent work, not safety
claims of this foundation. Rollback must stop token-owning runtimes before
dropping these columns.
