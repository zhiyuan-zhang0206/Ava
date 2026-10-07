# Ops: K8s semantics without K8s

## Context

The production incident lineage falls almost entirely on the ops line —
rollout blockers, migration collisions, quiesce races, redis ACL loss on
restart, pg password/bind drift, false-green probes, orphaned gateway,
phantom cluster births. Post-incident fixes kept accreting self-heal gates
onto whoever ticks (watchdog grew schema / pin-drift / stranded-pause
recovery; restarter grew health gating and orphan reapers), turning them into
an accidental orchestrator nobody designed. Desired state is expressed in at
least five places (`.env`, cluster registry, `cluster_pin`,
`build_services()`, hardcoded watchdog check lists, prose in AGENTS.md), which
already produced both predicted failure modes: dual-roster desync
(task-maintenance registered in `build_services()` but missing from the
watchdog hardcoded list — no keepalive) and reconcilers fighting (watchdog
force-checkout reverting a manual rollout fix per `cluster_pin`). The obvious
challenge was raised: are we rebuilding a worse Kubernetes — should we just
adopt it?

## Decision

**K8s the runtime: rejected for the current deployment shape.** Three
structural misfits: (1) macOS desktop coupling — the prod gateway host is
also the desktop-automation host (headed logged-in Chrome, TCC permissions,
AX automation, signed launchd helper); K8s on macOS lives in a Linux VM and
none of that can follow, so adopting it yields two supervision systems, not
one. (2) Agents operate the host by design; containerizing them means
privileged/hostPath escapes — isolation as theater. (3) Deployment is
source-checkout + uv self-update (PR → merge → `ava.self.update`), not
images; a registry/build pipeline in the self-update loop is a large new
liability. Empirical test: of ~11 incident classes, K8s structurally
eliminates ~2, half-helps 2, and swaps the git-pin rollout class for an
image-pipeline class; the majority (data-plane identity, application
semantics) are outside its jurisdiction. Re-evaluate if Ava targets a
headless Linux fleet, where the desktop half does not exist.

**K8s the semantics: adopted.** Ops is extracted into one module speaking
K8s vocabulary — Spec (single expression of desired state; `build_services()`
absorbed), Status (probes verifying the real contract, e.g. authenticate as
the actual role, not TCP liveness), Controllers (one reconciler per state
dimension with its own cooldown/backoff), a controller-manager tick (watchdog
slims down to running the controller list), CronJobs (pg-backup stops
piggybacking healthchecks; the debt sweeper gets scheduled), and Drain (agent
quiescing as a shared primitive for update and stop). Rollout goes
declarative: `ava update` = write the new SHA into spec, per-machine
controllers converge, rollout watches status — completing the half-converted
state (pin was already reconciler-driven while rollout stayed imperative;
that mismatch is what made them fight). Aligning our vocabulary with K8s
also lets model prior knowledge transfer to operating Ava.

**Module naming.** Reclaim `ops/`: evict the wire schemas it currently holds
(they belong to gateway), grow the real ops layer in place. Layering:
`shared < ops < {gateway, cli}`, formally inside the import-linter contract;
`services/` daemons become thin mains over ops controllers.

## Alternatives rejected

- **Adopt K8s/k3s now** — see misfits above; the ~100MB framing undersells
  the real cost (VM on macOS, image pipeline, split-brain supervision).
- **Stay imperative and keep adding per-incident self-heal gates** — growth
  without a declared spec is how the immune system became folklore.
- **A new module name** (`orchestration/`, `cluster/`) — `ops/` is the human
  word; evicting the squatters is cheaper than teaching a new name.

## Consequences

Final-state blueprint in `future/infra/ops-module.md` (spec content,
controller inventory, identity truth-table, probe audit, state-dimension
inventory). Implementation is left to Ava agents; sequencing deliberately
undiscussed.
