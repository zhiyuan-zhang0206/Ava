# The release/image update path is removed

## Context

PR #3479 landed a retained-image release path: image preparation and
verification, a frozen image-exec handoff, a finite external executor with
Linux and macOS launchers, a fleet coordinator with a per-unit enrollment
channel, per-rollout write-generation rotation with a database fence, and PITR
activation as a release-transition operation. It admits a fleet of one:
`require_topology` and `require_fleet_of_one` refuse any request that names
another unit.

The production cluster is networked (one Linux gateway, several macOS runners,
the cluster secret set) and has always run from source. The
[source-mode decision](2026-09-30-networked-cluster-stays-on-source-updates.md)
records why the release path cannot serve it: using it needs dbgen-8, FC-12 and
FC-11, about 2,500 to 3,500 lines, and two rounds of VM rehearsals, and K3 and
K4 would still fail every networked release. A cluster is updated by
`python -m cli.fleet_update` (stop, switch, start) and a stale process is kept
from writing by the
[client-side code-version gate](../execution/converge/2026-09-30-client-side-code-version-gate.md).
[Postmortem 0009](../../../../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)
states the rule the path fails: complexity must name the failure it prevents,
and this one was sized to a fleet-release system this cluster does not need.

The path was cut from production first: no production module imports it, and
an import-linter contract plus a dynamic boundary test held that edge at zero.
What was left was inert code with tests, five CI workflows, an operator runbook
and a second mode in the signed macOS helper.

## Decision

Delete the path as a whole, without a compatibility layer: `cli/release_*`
(prepare, operator, transition with its PITR operation, fleet, handoff, build),
the release inventory and plugin probe, the runtime release, prepare,
publication-input, service-identity and migration-context modules, the ABI and
plugin-inventory modules, the image-exec handoff contract, write-generation
fence and admission effects, PITR activation, the `shared/` release-probe shell,
the helper's `--finite-executor` mode, their tests, proof scripts and CI
workflows.

## Alternatives rejected

- **Keep it shelved.** It costs every lifecycle change a second implementation
  to keep compiling and proving, a Swift mode in a signed helper, and CI legs
  that ran over an hour, for a path no operator runs. Dead code with tests is
  the shape postmortem 0009 warns against; git history keeps it.
- **Finish the chain.** Roughly 2,500 to 3,500 lines and two VM rehearsal
  rounds to buy workload rollback and an atomic image swap, which an attended
  single-operator update handles by rerunning an idempotent half.
- **Keep the pieces that look reusable** (write-generation rotation, the PITR
  activation shell). The rotation orchestration existed to serve rollouts, and
  a leaked credential is rare and supervised, so it is a runbook procedure. PITR
  activation ran only as a release-transition operation.

## Consequences

- An update is `python -m cli.fleet_update`: a full outage, no automatic
  recovery and no migration rollback command, as the source-mode decision
  accepts.
- No command activates physical PITR. A home that is already activated keeps its
  archive settings and record, and the backup, upload, retention and restore-drill
  services keep running.
- Leaked-credential rotation follows the runbook, which drives the
  `base.cluster.authority` library directly. Nothing rotates the write
  generation on an update.
- The stale-process guarantee is the code-version gate, not a database fence.
- The macOS helper loses its finite-executor mode, so each Mac rebuilds and
  re-signs it at its next update. The signing identity is unchanged, so the
  granted permissions stay.
- The CI workflows `runtime-prepare`, `runtime-migration`, `runtime-frontend`,
  `runtime-wheel` and `release-store` are gone, and the failed-job rerun
  whitelist names only CI.
- A host's leftover `releases/` directory is no longer read.
- A later need for image updates (several units that must move together, a
  fleet large enough to want rollback) starts from a new design against that
  need rather than from this code.

Forward: [2026-10-03-retire-write-generation-rotation.md](../execution/converge/2026-10-03-retire-write-generation-rotation.md) reverses the
"keep the pieces that look reusable" choice for write-generation rotation.
