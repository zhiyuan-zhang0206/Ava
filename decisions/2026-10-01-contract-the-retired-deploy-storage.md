# The retired deploy and watcher storage is dropped

## Context

The release/image path, the managed-writer publication fence, the deployment
lease, the update straggler reap, the closed agent state and the watcher registry
were each removed from code by their own decision
([release path](2026-09-30-remove-release-image-path.md),
[publication](2026-09-30-remove-publication.md),
[lease](2026-09-30-remove-deployment-lease.md),
[straggler reap](2026-09-30-remove-straggler-reap.md),
[closed state](2026-09-27-terminate-has-no-closed-state.md),
[watchers](2026-09-27-watchers-are-never-restarted.md)). Every one of them kept
its schema objects and said the drop was a later migration, so that any single
update stayed reversible: the code that stopped using an object ships first, a
rollback of the contraction then lands on code that never touches the object,
and the down migration restores shape only.

The code half is done. What is left in the schema is unread and unwritten,
and the singleton rows carry values frozen at the last update of the retired
updater, which read like current facts (a `clean` outcome, a pinned commit) and
are not. [Postmortem 0009](../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)
names the rule: a mechanism that names no failure it still prevents is cost.

## Decision

Two migrations, each with a paired `.down.sql`, so each rolls back on its own.

`drop-retired-deploy-and-watcher-storage` (idempotent drops):

- the function `lock_runtime_publication_admission()` and its grant;
- `deployment_state`: every column except `id` and `min_code_version` (the lease,
  the settle note, the last-update outcome and the publication evidence);
- `host_deploy_state`: `updater_lease_expires_at`, `paused_at`, the five
  `stranded_hold_*` columns, and the `'converging'` posture value (rows at it
  become `idle`);
- the tables `cluster_pin` and `cluster_last_update`;
- `agents_meta.closed_at` and the table `agent_watchers`, with the runner grants
  on it.

`retire-restarting-and-publication-deferred`:

- `agents_meta.status` no longer admits `restarting`, and
  `last_admission_outcome` no longer admits `publication_deferred`. The
  migration refuses (and so rolls back whole) while any row is still in
  `restarting`; a `publication_deferred` observation is cleared, because the next
  admission overwrites it.
- `AgentStatus.RESTARTING` and `AdmissionOutcome.PUBLICATION_DEFERRED` leave the
  enums, the generated API types and the console. `AgentStatus` is a public SDK
  enum, so this is a contract change; the database value set and the enum move in
  one change because an unknown stored value fails fast at parse.
- The cold-prepare branch that recognised a completed legacy restart stranded in
  `restarting` goes with the value. The branch for an expired-lease idle row
  stays; it now only proves the cold END and writes nothing.

## Failures each object guarded, and why they no longer exist

| Object | Guarded against | Why it cannot happen now |
|---|---|---|
| publication lock function | a hosted runtime born between the old writer stopping and the new publication being recorded | an update stops every unit before it switches any checkout; a process that missed the stop is ended by the [code-version gate](2026-09-30-client-side-code-version-gate.md); there is no replacement window |
| `deployment_state` lease and settle columns | two updaters at once; a second update over unconverged hosts; a lease that lapsed mid-rollout | one attended script runs an update and refuses a host that holds a maintenance hold or whose HEAD disagrees; nothing holds a lease |
| `deployment_state` outcome columns, `cluster_last_update` | a failed rollout showing only as a commit mismatch, with no statement of what failed | the update script reports its own result and the operator reads it; no process records a result for another to read |
| `cluster_pin` | cluster drift from the intended commit; the pin controller forcing a checkout back | the script moves every unit to one named commit and never restores a recorded one |
| `managed_writer_evidence` | publication state | the fence is gone |
| `host_deploy_state` updater lease, pause anchor, stranded-hold record, `converging` | telling an alert that an updater was alive; scoping a pause window; remembering a pause whose owner died, with an automatic recovery budget | the updater and the pause controller are gone; the maintenance journal's exits are `ava maintenance resume --cancel` and `repair`; the posture row keeps `idle`/`paused` |
| `agents_meta.closed_at` | an agent woken by its own sessions after a final terminate | terminate has no closed state; ending the agent's shell sessions is the remedy |
| `agent_watchers` | rebuilding a killed watcher at the next boot | a watcher is a plain shell session and is never restarted |
| status `restarting` | the straggler reap's durable kill mark | the reap is gone, and the hosted restart applies without a status of its own |
| `publication_deferred` | an admission held while a publication was pending | the fence is gone |

## Kept

- `deployment_state` and its `min_code_version`: the version gate reads and
  raises it.
- `host_deploy_state` and its `idle`/`paused` posture: `ava stop`, `ava pause`,
  maintenance and `ava start` write it; the 503 middleware, `ava status` and the
  deploy window read it.
- `agents_meta.runtime_protocol_version` and the protocol v1 caller gate: whether
  v1 is activated or retired is a separate decision.
- The `inbound_messages` applied/observed/target columns and their commit-time
  guard: hosted lifecycle uses them.
- The event kind `restart_handoff_host_unhealthy`: retired kinds stay registered
  so historical events stay readable.
- `TerminationSource.INTEGRITY`: historical rows carry it.

## Alternatives rejected

- **Leave the schema as it is.** An unread column is a place a later change
  quietly starts reading again, and a singleton that holds the outcome of an
  update from before several later ones answers a question wrongly.
- **Move `restarting` rows to `idling` inside the migration.** It would replace
  an operator's judgement about a stuck row with one nobody verified. The
  migration refuses and the operator repairs the row.
- **Keep `RESTARTING` in the enum for old rows.** The enum parses stored values
  strictly, so a value the database refuses and the enum accepts (or the reverse)
  is a fault, not slack.
- **Restore the original column order on rollback.** The down migration adds
  columns back at the end; reordering would rebuild `agents_meta`, a hot table
  with many dependents, to recover an ordering every reader ignores by naming its
  columns.

## Consequences

- A rollback restores shape, not data: every returned column holds its default,
  the singleton tables return with their seed row, and `agent_watchers` returns
  empty. The operator keeps the rows the update would lose before it starts.
- Rolling back past these migrations needs the down files first, in reverse
  order and in one transaction, and only then the older code, because older code
  refuses a database that holds a migration it does not know.
- `ava.agents.AgentStatus` has three members. A script that names
  `AgentStatus.RESTARTING` fails on attribute access.
- The runner role no longer holds grants on `agent_watchers`, and the
  group-grant list no longer names the table or the function.

Forward: the down-migration plan in this record is superseded — migrations are
forward-only since 2026-10-02 ("No down migrations. A mistake is fixed forward
by a new migration"; merged migration files are immutable — see
[`migrations/README.md`](../migrations/README.md), commit 16e35c44e). Neither
migration below carries a `.down.sql` and none is planned, so the rollback
steps stated above (a rollback restores shape; rolling back past these
migrations needs the down files first) are void: recovery from these migrations
is fix-forward. The expand-contract ordering the Context describes still
stands.

Forward link (2026-10-03): `ava pause` was deleted; a stop with a different keep set replaces it. See [delete ava pause](2026-10-03-delete-ava-pause.md).
