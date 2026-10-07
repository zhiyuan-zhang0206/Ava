# A networked cluster stays on source-mode updates

## Context

The production cluster is networked: one Linux gateway unit under systemd and
several macOS agent-runner units (`launchd -> signed permissions helper ->
ava-root`), every unit with the cluster secret set and every unit running from
its `$HOME/.ava/source` checkout on a detached HEAD (source mode).

The release path (`ava cluster release request` -> `ava cluster update
--prepared`) admits a fleet of one: `require_topology` and
`require_fleet_of_one` in `cli/release_fleet/inventory.py` refuse any request
that names another unit. Letting the networked cluster use it was investigated
end to end (2026-09-29/30). It needs, in order:

- **dbgen-8** — per-unit write-generation delivery over the coordinator
  channel (capability exchange at unit `authorizing`, `unit.json`, converge
  issuance). Estimated at about 1,000 lines.
- **FC-12** — schema-changing releases (the `migrating` phase: expand-only
  rule, restore point, the previous image kept runnable on the expanded
  schema). Until it exists, any release whose packaged SQL differs refuses.
  In the 30 days before this ruling 78 migrations landed, so nearly every
  update carries one: FC-12 is a hard prerequisite, not an optimization.
- **FC-11** — every unit entering image mode. macOS first image selection is
  refused outright (`_host_supports_adoption` in
  `cli/release_operator/adopt.py` admits Linux only), so it is a whole new
  path, not a configuration step.
- A networked A->B->A rehearsal with a migration, before production.

Two further gaps would fail every networked release even after that chain:

- **K3** — the coordinator dials each unit's `/ops` through
  `ops/cluster_rpc.dispatch_to_machine` (`OpsTransport` in
  `cli/release_fleet/units.py`), which presents `gateway_auth_headers()`: in
  the executor's process that is the human cluster secret. With a secret set,
  a unit's ops server admits only its write generation's machine tokens, so
  the first `release_image_exec` gets 401.
- **K4** — a unit's finite executor carries no database login and no API
  token, so its quiesce (`ops/agent_pause` `prepare` / `drain`, as
  `cli/release_transition/local.py` runs it) cannot run.

Standing rulings frame the choice: single user, no backward compatibility in
code; cluster updates are attended, and code is written only for cases that
would wedge or put security or data at risk; the repository records no
deployment state.

## Decision

- Production stays in source mode. A networked cluster is updated by the
  attended stop / switch / start procedure, scripted as
  `python -m cli.fleet_update down` and `up` and documented in
  [the runbook](../../../../conventions/runbook.md#updating-a-networked-cluster-in-source-mode).
- The dbgen-8 -> FC-12 -> FC-11 -> rehearsal chain is not pursued, and K3 and
  K4 are not closed.
- The script never rolls back. The first failure stops the half; the operator
  fixes the cause and reruns the whole half, which is idempotent.

## Alternatives rejected

- **Finish the chain now.** Roughly a thousand lines for dbgen-8, the whole of
  FC-12, a new macOS adoption path and a rehearsal — and K3 and K4 still fail
  every networked release until they are closed. Updates are needed in the
  meantime.
- **Keep the procedure as a document only.** Each manual step already failed
  once in practice: a legacy `post-checkout` hook recursed on the detached
  checkout, `pause` instead of `stop` made start refuse, `uv sync` without
  `--frozen` rewrote `uv.lock`, and a start over SSH cannot sign the helper.
  A script enforces the order and checks each step's outcome.
- **A resumable state machine with per-step resume points.** Each half is
  already idempotent (`ava stop` and `ava start` on a unit already in that
  state succeed), so rerunning a half after a fix covers every interruption
  the attended operator meets; journaled resume would add state for no failure
  that rerunning does not handle
  ([postmortem 0009](../../../../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)).
- **A source-mode branch inside `ava cluster update`.** That verb is the
  release path's entry; a second update authority inside it would run from one
  unit's CLI and depend on that unit's configuration. A standalone operator
  script needs nothing from the operator's own cluster.
- **Rolling, one unit at a time.** The gateway's cold start applies pending
  migrations only once no runner is up, and old runner code refuses a newer
  schema (`CodeBehindSchema`); the outage is cluster-wide either way.
- **Automatic rollback in the script.** No command rolls migrations back
  (`base.deploy.schema.migrations.rollback_to` has no production caller). A
  scripted rollback would be a second, unexercised path; the attended operator
  decides.

## Consequences

- Every update is a full cluster outage: every agent drains, persistent
  terminals and schedules close (stop, not pause), the data plane restarts.
- There is no automatic recovery. A failure leaves hosts in mixed states
  (some stopped, some switched) until the operator repairs and reruns; a
  migration-bearing update has no rollback command.
- An update never rotates the write generation. A compromised unit or a lost
  capability bundle still cannot be contained in-band.
- Every macOS start needs a logged-in GUI session on that Mac.

The release/image path is to be deleted, and stale processes are to be kept
out by a version gate instead; see the follow-up decision.

Forward link (2026-10-03): `ava pause` was deleted; a stop with a different keep set replaces it. See [delete ava pause](../../../agents/graph/2026-10-03-delete-ava-pause.md).
