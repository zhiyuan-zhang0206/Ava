# Unified cluster lifecycle

This is the remaining implementation plan for the September 25 architecture
revision. Current implemented behavior belongs in
[Ava Root](../../services/ava_root/ava_root.ava.okf.md) and
[start identity](../../cli/start_identity.ava.okf.md). The earlier
[decision](../../decisions/2026-09-12-process-lifecycle-final-state.md) remains a
historical record; its same-PID replacement and intermediate migration paths
are not implementation requirements for this revision.

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
- A retained external update executor owns the release operation and outlives
  the application root it replaces. On macOS its placement must preserve signed
  helper ancestry. Neither the running old application nor a source checkout
  being modified in place is the executor's code authority.
- Preparation, native supervision and release decisions have separate owners.
  Preparation cannot secretly stop services; readiness cannot publish a release;
  the supervisor cannot choose an upgrade or rollback target.

## Remaining implementation

1. Finish native root and resource custody on each supported platform. Prove
   deliberate shutdown does not fight OS restart policy, and test owner death,
   interrupted spawn and unresponsive IPC. Windows terminal birth must originate
   outside application Jobs through a registered resource owner; ordinary
   execution cannot use that route as an untracked escape.
2. Connect immutable candidate preparation to the external executor. Capture the
   complete intended roster and artifact inputs before stopping work. Resolve
   the candidate once and use captured executable paths, not moving selectors.
3. Replace legacy update/rollback orchestration with one durable state machine:
   prepare, quiesce, close old writers, migrate, select, start, observe, commit or
   recover. Every interrupted phase must be inspectable and repeatable without
   manufacturing a second operation or replaying an uncertain external effect.
4. Close fleet write authority before schema changes. An offline runner is not a
   stopped writer. Rejoining stale releases remain fenced until they converge.
   Revoking DB access does not prove external code execution has stopped; the
   operation must also resolve execution custody. Include durable schedule/PTY
   workloads in the writer inventory: a schedule runner can outlive application
   root and access the database through its own process. Root exit alone is not
   a migration barrier.
5. Exercise upgrade and recovery in disposable clusters, including paired
   migrations and writes made after upgrade. A down migration alone does not
   prove those writes remain usable by the retained release.
6. Remove all superseded runtime paths and perform one explicit cutover. Temporary
   manual database or host repair can belong to that cutover record, never to a
   permanent compatibility layer. Record each host's approved executable search
   directories as `AVA_SERVICE_PATH` in its home configuration before first start
   with the new code; omit virtualenv directories. Do not derive this declaration
   from a later recovery caller's PATH. Deployment is separate from PR merge.

## Planned: remove retired controller storage

The old controller graph, hold-watchdog state machine, stranded-hold writers,
alerts and status fields are removed from code. A separate explicit cleanup
migration/cutover must remove the now-unused `host_deploy_state` columns
`stranded_hold_since`, `stranded_hold_reason`, `stranded_hold_attempts`,
`stranded_hold_attempted_at`, and `stranded_hold_recovery_note`. No current
reader or writer treats these historical values as maintenance authority.
Physical schema deletion is not implemented in this slice.
The same cutover must retire `cluster_last_update` and the unused updater-outcome
columns in `host_deploy_state`. Their controller producers and status projections
are removed together; historical values must not masquerade as a current release
operation. Preserve active maintenance, publication and deployment guards until
their replacement authority is implemented and verified. Mixed deployment-clock
and telemetry helpers still serve live deployment-window settlement; deleting
their retired callers does not authorize removing those shared guards.
The cutover must also reconcile any historical `deploy-probe` alert named
`update failed: host left held`; its retired heartbeat writer no longer resolves
such records. No runtime alert cleanup was performed by the source deletion.

## Planned: runtime birth and database write admission

Local startup readiness and fleet write authority are distinct proofs. Bind a
successful local start to the actual root process birth, launch-input digest and
loaded runtime identity. Authenticate root IPC peers using native process facts;
the socket path or a JSON response alone cannot establish identity. Source
development and immutable images have explicit identities. A missing identity
never grants birth or recovery. Verify the agent-host also belongs to that root's
captured service generation before it admits an agent incarnation.

Retired process-runtime termination capture and observation are removed from the
active lifecycle. Resurrection requires retained hosted generation/owner and
settled command state. Historical process/unknown rows and incomplete hosted
identities refuse pending explicit cutover reconciliation. Hosted termination
follows continuation/resource settlement, never agent-host process exit.

This local proof does not replace the surviving database publication fence.
The current resource path still permits legacy protocol-zero rows with unknown
`incarnation_resources`, and ordinary spawn does not produce `ResourceBirth`.
Consequently the completed-work A/B/A proof cannot certify in-flight execution,
managed resource admission, or fleet writer closure. Do not enable protocol one
merely because local root evidence is available.

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
after this boundary is proven can the old publication journal, preparation
grants, legacy selector parser, protocol-zero fallback and their dead controllers
be deleted together. No permanent row-adoption compatibility path belongs in the
new runtime.

### Proposed database authority boundary

Separate schema compatibility from write authority. A same-schema patch must
still fence an offline old runner. Each rollout direction receives a fresh pair
of restricted application logins, one gateway and one runner, inheriting stable
NOLOGIN capability groups. Runtime logins own no schema objects; operator and
migration authority remain separate. Returning to image A uses a fresh write
generation, never A's revoked credentials. A retry reconciles its exact existing
unrevoked generation instead of issuing another pair. Persist that generation
before any candidate can authenticate, including candidates that fail readiness.

After bounded business quiescence, revoke previous logins, close the owned
pooler and verify old database sessions and prepared transactions are resolved.
Only then admit the replacement generation. Database access is needed for
startup readiness; business admission remains held until readiness completes.
Role cleanup may retain inert failures, but cannot revive revoked credentials
or delete dependent objects with CASCADE to meet a catalog-size target.

Deliver capability material only through the verified launch of the captured
unit/image. Include the generation in its immutable launch proof; never swap
credentials underneath an old environment digest. Retire the bootstrap endpoint
that can exchange an old bearer for fresh database credentials. Offline units
must converge before receiving current authority. The local transition does not
yet implement this fleet delivery channel. Direct database fencing also does not
revoke a stale caller's HTTP bearer; API admission requires its own explicit
generation boundary before claiming complete stale-writer exclusion.

PostgreSQL and pooler connections always authenticate, even when the
frontend/control-plane bearer is empty
([decision](../../decisions/2026-09-26-internal-data-plane-always-authenticated.md)).
The empty-secret convention in AGENTS.md changes when this lands.
No database role, credential, schema or runtime-admission cutover is implemented
by this plan.

## Remaining: qualify PITR custody and restart recovery

Typed release and PITR operations now use the same home journal and finite
executor. The connected implementation is documented in
[retained release transition](../../cli/release_transition/release_transition.ava.okf.md).
PITR reserves the home before configuration mutation and uses the ordinary
retained-image start boundary; the retired cluster-update restart seam is not
its fallback.

Qualification still requires a real retained-image PostgreSQL restart and
WAL-settings proof, including activation, interruption at each durable boundary,
serialized rollback and preservation of backup data. The completed-work
same-schema release cycle does not establish these PITR guarantees.

Worker custody must distinguish an actual retained direct child from a durable
process receipt. Only the direct owner may use its unreaped group leader to
close a kernel process group before reaping. A session can contain several such
groups. A saved PID/PGID, a timeout, or an empty process census alone cannot grant
closure or permit deletion of partial backup/restore evidence. The shared direct
launch boundary now retains the actual child handle and refuses reaping after
uncertain closure; its PITR and logical-backup consumers retain unresolved
evidence. Each PITR/backup operation is now one directly owned worker process
group with no subgroups
([PITR operation custody](../../services/pitr/operation-custody.ava.okf.md)).
The remaining custody gaps: controller death on plain POSIX/macOS has no
platform owner (for example a Linux cgroup) able to prove closure without the
original controller, so retirement past that point still needs a human; and a
macOS late-fork race in `shared/exec_process_domain.py` is being qualified
separately. Do not add another restart or cleanup fallback, or infer closure
from another process census.

## Remaining: macOS finite executor

Implemented (see [macOS executor custody](../../cli/release_transition/launcher_macos.ava.okf.md)):
one finite launchd job per operation attempt runs `--finite-executor` of the
same stably signed helper artifact the live home helper runs, admitted by
`codesign -R` against the stable requirement and identified through the
kernel's socket peer. The executor and its finite tools inherit the job's
process group; the helper re-executes without launchd's session environment,
publishes its group before spawning, keeps its direct child until reaped and
closes its own group (TERM, then KILL) before exiting, because launchd's own
cleanup is a single SIGTERM to the group. `native.py` is the one adapter
dispatch; the release journal records helper birth, executor birth and
terminal evidence separately and binds a receipt to its attempt. `launchctl
print` readback is an explicit contract for the measured macOS 26 format:
missing fields, unknown formats, pending spawns, failed queries and non-exact
absence retain custody. Every terminal closure proves the group empty;
survivors are killed only while the recorded executor pins the group, else
closure refuses with their evidence. A reboot, or a new login session with the
recorded owners gone and the group empty, is positive closure. Opt-in native
tests on 26.6.2 cover natural exit, lost bootstrap response, concurrent launch,
executor and helper KILL, SIGTERM-ignoring members, SIGUSR1/2 terminals,
continuation, stable-identity signing and the escaped-group negative control.

Pending before macOS admission:

- Release start through the persistent home helper: the `root_service` /
  `stage` branch (helper-seeded root birth, helper-parent readiness proof,
  pinned seed as steady state). Until then macOS submission refuses before
  reservation.
- A descendant that creates its own group or session is visible only while
  its parent lives and survives job cleanup. Automatic recovery after such an
  escape needs a stronger owner boundary (for example the job's resource
  coalition, which is inherited but read only through private `proc_info`
  flavors); PITR and any group-creating tool stay unadmitted on macOS.
- A job-group member that outlives both recorded owners after an external
  helper kill cannot be proven to belong to the attempt; recovery stays an
  operator action named by the refusal.
- Signed-helper release A/B/A on macOS with a real home helper, including
  candidate failure, previous-image start and native logout/shutdown
  interruption (logout and reboot recovery are unit-tested only).

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

The independent health-probe rollback and known-good writers are removed.
The new executor's frozen-cohort workload threshold and known-good publication
policy are still unimplemented pre-cutover work. Health alerts remain active;
removing an old decision path is not evidence that its replacement is complete.

Current local serving proofs do not complete this plan. Acceptance requires
normal teardown, interrupted recovery and release A/B/A evidence, removal of
legacy paths, reviewed changes and green CI before the production cutover.

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
