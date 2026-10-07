# A row this runtime ended unowned stays resurrectable

## Context

Resurrection accepts a terminated row only with positive evidence that no
earlier incarnation can still write: a retained hosted identity, or a
fresh-INSERT birth marker proving the row was never admitted
(`ops.resurrection_retry.hosted_resurrection_target`). Everything else
refuses with `runtime_cutover_required`.

The runtime itself leaves rows with no runtime identity:

- a new agent before its first admission;
- a resurrected agent before it is admitted again;
- an agent whose restart applied, before its successor is admitted;
- an agent whose straggler-reap mark was settled.

A force terminate is the only writer that ends such a row. The reapers
require a hosted kind, and every other terminating path keeps the identity.
Forces happen whenever an operator kills an agent that never came up, and
`ava cluster pause` forces every row of the machine, including a whole cohort
that `ava pause` or `ava stop` had drained. Each such row was refused for
good.

The birth marker cannot cover these rows. Spawn does not stamp it: a marker
turns the first admission into a managed resource set, which bypasses the
publication/all-writer boundary, and the drain parks only NULL-resource rows.
Under protocol zero, admission also keeps NULL resources NULL, so a released
row's resources rarely name its predecessor.

The row shapes are also identical to what the retired runtime left: agents
drained, restarted or resurrected before the cutover. Those remain unknown
historical runtimes
(`tests/agent/test_resurrect_lifecycle_fence.py::test_force_on_unadmitted_row_cannot_create_a_hosted_successor`).
No existing column tells the two apart by its existing values.

## Decision

A force records `unowned_termination` on its own terminate command when, under
the agents_meta row lock and before its terminated write, three things hold:

- the row is idling;
- it has no runtime kind, generation, owner or pid;
- its unowned state has a lifecycle origin
  (`shared.lifecycle_acceptance.record_unowned_termination`).

The origin is one of two facts only this runtime writes. Both are new values
in existing columns:

- **Birth.** The spawn INSERT records the first life's epoch fence as
  `last_resurrect_inbound_id = 0`. Every reader already treats 0 like NULL
  (nothing predates it). No retired runtime stamped a birth epoch.
- **Release.** A `lifecycle_release: true` payload key on the command whose
  transition left the row unowned: the resurrect inbound, an applied restart,
  or a restart closed by a straggler settlement.

An origin accumulates per agent, so one is enough. After the first mark, only
this runtime's own transitions can clear the identity again. A row the
retired runtime terminated cannot gain a mark before this runtime resurrects
it: releases act only on live rows.

Resurrection accepts a row with no runtime identity when a receipt exists in
the current life, that is, with an id above `last_resurrect_inbound_id`. It
resurrects the row as a fresh hosted birth. The final CAS rechecks the
receipt by id. The receipt is an immutable command row, so the transaction's
own resurrect inbound and fence cannot satisfy that recheck.

Resurrection restores the unowned state the force ended. Whether that state
runs is still decided by admission: the publication decision, the maintenance
hold, the managed-resource predecessor rule and the owner CAS are unchanged.

## Alternatives rejected

- **Stamp the birth marker at INSERT.** It enables managed resource births
  without the publication boundary, breaks drain parking, and does not cover
  released rows.
- **Take (G, O) from the resources.** Under protocol zero they are NULL for
  nearly every row.
- **Accept any force of an unowned row.** This is simpler, but it also revives
  legacy unowned rows, which the cutover left unknown.
- **A new column (an ownership-release timestamp or flag).** It is a schema
  change, which the cutover's A/B/A pairing and the SQL-unchanged release
  path cannot take.
- **Probe the host for the agent's processes at resurrection.** That is a
  permanent legacy compatibility path in the runtime
  ([2026-09-28](2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md),
  e3).

## Consequences

- An agent this runtime spawned, resurrected, released or settled, and then
  force-terminated before admission, resurrects by chat, manual resurrection
  or the watchdog. After `ava cluster pause`, this includes a machine's
  drained cohort.
- A legacy row that this runtime has not admitted since the cutover still
  refuses after a force. So does a row terminated before the fix shipped, or
  by a rollback to a release without it.
- The cutover survey (D-8) classifies by shape. It still lists receipt-bearing
  rows terminated after the attestation as `after_attestation`, although the
  runtime resurrects them.
- A managed row whose applied restart a force superseded loses that
  predecessor receipt. It resurrects, but while the host process its
  resources record is alive, the successor's admission fails:
  `admit_resources_async` raises `ResourceEvidenceError` (no complete
  predecessor closure) outside the conversion to `resource_fence`, the
  dispatcher logs `host_turn_crashed` on every wake, and no
  `last_admission_outcome` is written. Once that host process has exited (a
  host restart), the dead-empty-host proof admits the successor. Protocol-zero
  rows (NULL resources) never take this path. The predecessor rule is
  unchanged here; recording that refusal as `resource_fence` would change
  every predecessor-closure refusal of admission, so it is a separate change.
- `0` is a valid value of `last_resurrect_inbound_id`: born under this
  runtime and not resurrected since. A future `IS [NOT] NULL` reader of the
  column (such as "was resurrected"), a foreign key or a `CHECK (> 0)` must
  account for it. The column's comments in `db/schema.sql` (inline and
  `COMMENT ON`) still say only a resurrection writes it and NULL means none
  was recorded; changing them needs a migration, so that rides the first
  migration after the cutover.
- The origin and the receipt live in `inbound_messages` rows:
  `lifecycle_release` on `resurrect` and `restart` rows, `unowned_termination`
  on `terminate` rows. Nothing deletes inbound rows today. A future inbound
  retention must keep these rows; otherwise resurrection falls back, closed,
  to refusing the rows they vouched for.

Forward link (2026-10-03): `ava pause` was deleted; a stop with a different keep set replaces it. See [delete ava pause](../graph/2026-10-03-delete-ava-pause.md).
