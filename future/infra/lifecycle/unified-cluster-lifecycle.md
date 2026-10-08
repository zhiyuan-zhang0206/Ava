# Unified cluster lifecycle

This is the remaining implementation plan for the September 25 architecture
revision. Current implemented behavior belongs in
[Ava Root](../../../services/supervision/ava_root/docs/ava_root.ava.okf.md) and
[start identity](../../../cli/docs/start_identity.ava.okf.md). The earlier
[decision](../../../docs/decisions/runtime/processes/startup/2026-09-12-process-lifecycle-final-state.md) remains a
historical record; its same-PID replacement and intermediate migration paths
are not implementation requirements for this revision.

## Status: the retained-image release path was removed

[The decision](../../../docs/decisions/runtime/updates/release/2026-09-30-remove-release-image-path.md) deleted the
image-based update path this plan grew around: image preparation, the frozen
image-exec handoff, the finite external executor and its launchers, the fleet
coordinator and its enrollment channel, per-rollout write-generation rotation
and the PITR activation transition. A cluster is updated from source by
`python -m cli.fleet_update` (a down and an up half) behind the code-version
gate. Statements below about that path are void and the sections that only
planned it are gone. What stays live: root and resource custody (item 1),
the database authority that ordinary start uses (groups, generation 0, API
admission) and the retired-storage cleanup. [A second
decision](../../decisions/2026-09-30-remove-publication.md) deleted the
managed-writer publication fence: hosted admission reads no deployment-wide
state.

## Boundaries

- One idempotent `ava start` owns initialization, preparation and readiness.
  Existing identity, credentials and selected services survive an omitted option.
  No install/enroll wrapper or alternative service launcher remains at cutover.
- One root owns ordinary application services per machine and home. macOS has
  `launchd -> signed helper -> ava-root`; Linux has `systemd/caller -> ava-root`.
  Windows has its native launcher followed by root. Only macOS uses the helper
  as an application permission ancestor. Root is not a privileged Unix account.
- Application replacement and durable resource lifetime are distinct. Database
  processes and deliberately persistent terminals need explicit native custody
  that survives application replacement. An unresponsive process or port alone
  never establishes absence, ownership, or successful recovery.
- Preparation, native supervision and release decisions have separate owners.
  Preparation cannot secretly stop services; readiness cannot publish a release;
  the supervisor cannot choose an upgrade or rollback target.

## Remaining implementation

1. Finish native root and resource custody on each supported platform. Prove
   deliberate shutdown does not fight OS restart policy, and test owner death,
   interrupted spawn and unresponsive IPC. Windows terminal birth must originate
   outside application Jobs through a registered resource owner; ordinary
   execution cannot use that route as an untracked escape.
2. Removed: candidate preparation, the durable release state machine and
   fleet write authority were the image path's items; the decision replaced
   them with the scripted source update. What it does not do (in-flight
   workload rollback, automatic recovery) is an operator step in the runbook.
3. Remove all superseded runtime paths and perform one explicit cutover. Temporary
   manual database or host repair can belong to that cutover record, never to a
   permanent compatibility layer. Record each host's approved executable search
   directories as `AVA_SERVICE_PATH` in its home configuration before first start
   with the new code; omit virtualenv directories. Do not derive this declaration
   from a later recovery caller's PATH. Deployment is separate from PR merge.
   The one-time home adoption and database-records repair tooling is removed:
   no code adopts or converts an existing home or its database records.
   Cutover preconditions and one-time repairs:
   - Every cluster reads `deployment_state.managed_writer_evidence->'pending'
     IS NULL` before this release is admitted. The retired updater's checked
     publication recovery is gone, and no runtime command clears a recorded
     pending publication; an operator's manual database repair clears exactly
     the recorded value.
   - `$AVA_HOME/installed_sha` has no reader or writer; the source-tree check
     alerts only on checkout edits of a source-run home. Delete the file in the
     cutover record.
   - Do not remove `$AVA_HOME/run/deploy-pause-owner.json` on a host while a
     legacy updater, rollout, cluster-restart or hold-recovery session, an `ava
     stop` may still be alive there. No current
     code spawns a legacy session, and no command clears a `paused` record one
     left: once `ava status` shows no live owner, the operator
     removes the journal by hand.
   - `$AVA_HOME/run/updater-handoff.json`, `updater-handoff.lock`,
     `updater-bootstrap-recovery.json` and `updater-spawn/` have no reader or
     writer: recovery no longer refuses on them. Delete them in the cutover
     record.
   - `$AVA_HOME/deploy-state.json`, the retired updater's Gate marker, has no
     reader or writer; Gate never renders an update page. Delete it in the
     cutover record.

## Retired controller storage

The old controller graph, hold-watchdog state machine, stranded-hold writers,
alerts and status fields were removed from code, and migration
`20261001T055030_drop-retired-deploy-and-watcher-storage` dropped their storage
([decision](../../../docs/decisions/runtime/updates/release/2026-10-01-contract-the-retired-deploy-storage.md)).

Not done: the cutover must also reconcile any historical `deploy-probe` alert
named `update failed: host left held`; its retired heartbeat writer no longer
resolves such records. That is alert data, not schema, and no runtime alert
cleanup was performed by the source deletion.

## Planned: unowned-termination follow-ups

Three changes the
[unowned-termination decision](../../../docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md)
left for later:

- Record a predecessor-closure refusal as a durable `resource_fence`. Today a
  managed row whose applied restart a force superseded resurrects, but while
  the host process its resources record is alive `admit_resources_async`
  raises `ResourceEvidenceError` outside that conversion: the dispatcher logs
  `host_turn_crashed` on every wake and no `last_admission_outcome` is
  written. The conversion changes every predecessor-closure refusal of
  admission, so it is its own change.
- In the first migration after the cutover, restate the inline and
  `COMMENT ON` comments of `agents_meta.last_resurrect_inbound_id` in
  `db/schema.sql`: `0` is a valid value (born under this runtime, not
  resurrected since), not only an id a resurrection wrote.
- Any inbound retention keeps the `inbound_messages` rows that carry
  `lifecycle_release` (on `resurrect` and `restart` rows) or
  `unowned_termination` (on `terminate` rows). Deleting them makes
  resurrection refuse, closed, the rows they vouched for. `db/schema.sql`'s
  "inbound retention must not erase lifecycle intent" covers the fences, not
  these markers.

## Planned: runtime birth and database write admission

Local startup readiness and fleet write authority are distinct proofs. Bind a
successful local start to the actual root process birth, launch-input digest and
loaded runtime identity. Authenticate root IPC peers using native process facts;
the socket path or a JSON response alone cannot establish identity. The
source checkout has an explicit identity. A missing identity
never grants birth or recovery. Verify the agent-host also belongs to that root's
captured service generation before it admits an agent incarnation.

Retired process-runtime termination capture and observation are removed from the
active lifecycle. Resurrection requires retained hosted generation/owner and
settled command state. Historical process/unknown rows and incomplete hosted
identities refuse pending explicit cutover reconciliation. Hosted termination
follows continuation/resource settlement, never agent-host process exit.

The current resource path still permits legacy protocol-zero rows with unknown
`incarnation_resources`, and ordinary spawn does not produce `ResourceBirth`.
Consequently a local root proof cannot certify in-flight execution,
managed resource admission, or fleet writer closure. Do not enable protocol one
merely because local root evidence is available.

No activation path exists: every admission writes protocol zero. The retired
updater's activation chain (the pending journal's migration receipt, selector
change, normal-service readbacks and the `current` commit recording the verified
activation) bound per-service session readbacks and a version-2 selector that
the release path does not produce; it is not a dormant implementation to
reconnect. Protocol one would be a new design: the release/fleet path it was to
ride on was removed. The publication storage helpers, the barrier and the
runtime-admission decision are deleted.

The replacement must preserve one transaction for runtime ownership and resource
admission. A fresh metadata INSERT may stamp `ResourceBirth`; an existing agent
requires explicit predecessor and allocation closure. Maintenance drain must
verify the complete recorded resource set, never treat NULL as an empty set.
Retain the existing database-row-before-local-file lock order. A held-operation
continuation is bound to the exact maintenance owner, timestamp, frozen cohort
command and database target generation; it cannot mint a successor incarnation.

At the one-time cutover, inventory all old writers and independent execution
domains, close their native custody and fence stale database credentials where
needed. Retain the evidence used to reconcile each existing metadata row. Do not
mass-convert unknown NULL resources into `{}` or a new-agent birth marker;
unresolved rows remain inadmissible. Install the database contract that rejects
incompatible future writers before admitting the replacement protocol. Only
after this boundary is proven can the preparation grants, legacy selector
parser, protocol-zero fallback and their dead controllers be deleted together. No permanent row-adoption compatibility path belongs in the
new runtime.

Implemented for existing agents (FC-4a; why: `docs/decisions/agents/lifecycle/2026-09-27-existing-agent-closed-predecessor-admission.md`):
a retired-shape row is admissible only in the closed-predecessor form,
`IncarnationResources(G, O, host_process=null, requests={})` for the
incarnation the retired value names, backed by that incarnation's existing
predecessor receipt (the old drain's applied restart, or an observed
terminate); admission keeps `PREDECESSOR_RECEIPT` as its only rule. No code
converts a row to that form. Retired-shape rows refuse with `resource_fence` /
`runtime_cutover_required`; NULL resources stay protocol zero. Drain certification accepts
the complete empty recorded set of the released incarnation. A never-admitted
row resurrects as a fresh birth only with its birth marker intact, or when the
runtime's own force recorded that it ended the row unowned after the
runtime's own lifecycle had left it so
(`docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md`).

Implemented for agents terminated before the runtime incarnation (why:
`docs/decisions/agents/lifecycle/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`):
a terminated row with NULL resources and an incomplete runtime identity
resurrects only when it carries a minted hosted identity (fresh generation and
owner, no pid), which only passes the resurrection gate and which the
resurrection CAS clears. Resources stay NULL. No code mints such an identity;
every other such row keeps refusing.

### Database authority boundary

Per-rollout write-generation rotation belonged to the removed path. The ledger
keeps generation 0 and nothing replaces it
([decision](../../../docs/decisions/runtime/updates/execution/converge/2026-10-03-retire-write-generation-rotation.md)).

PostgreSQL and pooler connections always authenticate, even when the
frontend/control-plane bearer is empty
([decision](../../../docs/decisions/data/security/2026-09-26-internal-data-plane-always-authenticated.md)).
Implemented for a single local plane: Redis always authenticates with generated
admin and runtime passwords, which do not rotate per rollout; `pg_hba` admits
the OS-user administrator by `peer` and every other role by SCRAM; PgBouncer
always uses SCRAM against the active generation's verifier userlist and
restarts on a userlist change. The schema owner is `NOLOGIN`, the capability
groups carry every grant, birth mints write generation 0
([authority](../../../base/cluster/authority/docs/authority.ava.okf.md)), an ordinary
start re-grants and checks the catalog invariant, the root launcher
delivers each service its class login bound into the launch digest, and an
admitted operator CLI consumes the gateway login. Bootstrap serves no database
credential; a remote agent-runner installs a sealed, unit-bound capability the
gateway operator issues (`ava cluster db-authority issue-unit`,
`ava init --db-capability`, and `install-unit` for a later bundle), carrying the active generation's runner login,
and its API admission
([unit capability](../../../base/cluster/authority/docs/wiring.ava.okf.md#remote-agent-runner-units)).
The generation also carries one machine API token per class; the gateway admits the human secret or the ACTIVE
generation's tokens, a unit's ops server its generation's two tokens, and the
launcher delivers each service its class token (`AVA_API_TOKEN`) only while the
API is authenticated. Remote units never hold the human secret (bootstrap does
not serve it); their OTLP relay uses a telemetry token derived from it
([API tokens](../../../base/cluster/authority/docs/api-tokens.ava.okf.md)).
A home born before this model is refused; no conversion exists
([credential split](../../../docs/conventions/data/data-plane-secret-split.md#homes-born-before-this-model)).

## Verification and recovery policy

CI owns deterministic lifecycle, interruption, upgrade and rollback contracts.
Branch preview owns real topology and workload checks on the exact unmerged
input using the same lifecycle. Native Windows/Linux CI and macOS execution are
required for their respective process contracts; mocks cannot certify them.
Production observation is the final check against real workload behavior.

Quiescing has a bound: allow the initial drain, request the existing cancellation
mechanism with a system reason, then escalate only over proven owned execution
domains. Cluster progress cannot wait indefinitely for one agent. Record enough
durable state for recovery after interrupted work.

The initial workload rollback threshold is more than 20 percent of the frozen
eligible active-agent cohort, with an override for shared core failure. Individual
runner/agent failure needs bounded recovery and an alert even below that threshold.
Missing observations are unknown, not healthy. A degraded release cannot become
last-known-good. Alert delivery is configurable, including a user-selected agent;
the alert recipient does not become the automatic recovery authority.

The independent health-probe rollback and known-good writers are removed. Health
alerts remain active; removing an old decision path is not evidence that a
replacement is complete. A networked cluster has no in-band way to rotate its
write generation, so a compromised unit or a lost capability bundle is
contained by the manual procedure in the runbook
([what a bundle exposes](../../../base/cluster/authority/docs/unit-bundle.ava.okf.md)).

No real-process test runs a gateway with its cluster secret set against a
runner's real `/ops` and spawns an agent through it. The e2e stack blanks the
secret (`tests/e2e/conftest.py`): its directly started services mint no write
generation, and with a secret set `/ops` accepts only a generation's machine
tokens, so every spawn would be refused. The authenticated path is covered
only piecewise: the gateway and ops acceptance matrix in
`tests/components/lifecycle/db_authority/test_api_tokens.py`, run against an in-process
app and a bare ops listener. This is a known gap and a hard gate of the production
cutover rehearsal: the rehearsal runs on a cluster with its secret
set and proves gateway -> `/ops` -> spawn end to end before production is
switched.

Current local serving proofs do not complete this plan. Acceptance requires
normal teardown, interrupted recovery, removal of legacy paths, reviewed
changes and green CI before the production cutover.

## One-time retirement of historical update alerts

Before dropping the retired stranded-hold columns, retain the exact unresolved
alert instances with source `deploy-probe` and alertname
`update failed: host left held`, including their ids, fingerprints, starts_at,
labels, annotations, notification state and timestamps, plus the matching host
stranded-hold evidence. Verify the retired writers are stopped across the
affected hosts. Inspect each home's current maintenance generation, owner,
operation and readiness; historical columns cannot establish current recovery.

Resolve reviewed obsolete instances once by id or `(fingerprint, starts_at)`,
using an explicit detector-retirement note and preserving the existing history.
Require a transactional candidate count and readback; retry only unresolved
instances. A current outage remains a separately tracked live incident, never
reported as recovered because its old detector was deleted. Do not install a
recurring compatibility grader or resend historical notifications. This is a
cutover data action, not something performed by this development refactor.
