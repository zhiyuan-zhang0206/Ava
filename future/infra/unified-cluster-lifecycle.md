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
   operation must also resolve execution custody.
5. Exercise upgrade and recovery in disposable clusters, including paired
   migrations and writes made after upgrade. A down migration alone does not
   prove those writes remain usable by the retained release.
6. Remove all superseded runtime paths and perform one explicit cutover. Temporary
   manual database or host repair can belong to that cutover record, never to a
   permanent compatibility layer. Deployment is separate from PR merge.

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

Current local serving proofs do not complete this plan. Acceptance requires
normal teardown, interrupted recovery and release A/B/A evidence, removal of
legacy paths, reviewed changes and green CI before the production cutover.
