# Ops module

The ops layer owns desired service state, lifecycle operations and deployment
coordination. Its design rationale is recorded in
[the ops decision](../decisions/2026-07-19-ops-k8s-semantics-without-k8s.md).

| Responsibility | Current implementation |
|---|---|
| Service specification | `ops/spec.py` (capability selection and gates); `ops/roster/` (canonical roster, its `service_spec` contract) |
| Observation and status | `ops/roster/observe.py`; `ops/cluster_status/` (host snapshot, `schema_mismatch` diagnosis) |
| Native agent drain | `ops/agent_pause/` (drain, `probe` of the running host) |
| Agent lifecycle | `ops/agents/` (birth and wake); `ops/ops_lifecycle/` (lifecycle RPC ops) |
| RPC | `ops/rpc_schemas/` (wire vocabulary); `ops/cluster_rpc.py` (gateway client) |
| Pause, stop and restart | CLI maintenance orchestration over the shared drain |
| Release transition | `cli/release_transition/`, prepared image and native executor |

Each package keeps the import path of the module its door grew from, so
`ops.agents`, `ops.ops_lifecycle`, `ops.rpc_schemas`, `ops.cluster_status`,
`ops.agent_pause` and `ops.roster` read the same to their importers. Two
modules stay top-level by contract: the runtime-prepare probe imports
`ops.spec` by name inside a candidate image, and `python -m ops.private_files`
is an operator entry point beside its `private-files/` manifest.

`build_services()` supplies the application root manifest and local status roster.
Agent-runner units execute agents inside one agent host. There is no per-agent
process launcher or restarter service. Native service sessions and persistent
PTY hosts remain separate execution resources.

The application root owns service supervision. There is no controller manager,
background checkout/update trigger, scheduled updater reaper, or automatic
stranded-hold restart path. Release decisions belong to the finite executor.

Pause and update hold admission and wait for native restart, checkpoint flush,
actual continuation completion and resource settlement. Ordinary stop shares
that drain and then closes the selected local services, PTYs and data plane.
Timeout fails without implicit force. The complete operator contract is in
[graceful maintenance](../conventions/graceful-maintenance.md).

The current release adapter admits one local gateway on the same SQL schema.
Fleet distribution, schema migration, and workload-cohort release decisions
remain pre-cutover work in the
[unified lifecycle plan](../future/infra/unified-cluster-lifecycle.md). Retired
updater RPCs cannot be used to fill those gaps.

The native OS unit supervises the application root, which owns its service
subprocesses. Agent shells use independent PTY hosts. Stop verifies captured
process identity before signalling.

The import boundary is `shared < ops < {gateway, cli}`. The supported RPC
vocabulary lives in the `ops/rpc_schemas/` door; focused agent contracts live in
its `terminate`, `content`, and `billing_recovery` submodules. Gateway-only
schemas stay in `gateway/schemas/`.
The client and daemon reject unknown kinds before machine lookup, maintenance
admission, dedupe, or dispatch. `release_image_exec` is the frozen v1
image-exec handoff (`ops_cluster.release_image_exec_op`): the unit verifies a
prepared image in its own store and runs one fixed entry of that image with
bounded time (contract in `shared/api_contracts/release_handoff.py`, described
in `cli/release_handoff/release_handoff.ava.okf.md`). Retired updater fetch, prepare, bootstrap, and
continuation requests have no wire registration or handler. Their handlers, schemas, command producers and session/log status projections
are absent. Callers import live pause, status and recovery definitions directly.
The shared database publication and maintenance fences still enforce admission;
removing an updater command does not remove those guards.
Cluster identity remains the installed home path, resolved before runtime
configuration construction.

The host status snapshot's `paused` field includes native maintenance admission
and startup that has not reached `start-serving` readiness, in addition to the
business DB posture; `paused_reason` names the first true clause (`no_state` /
`business_pause` / `maintenance` / `startup`), so a failed start that parked the
serving gate reads apart from a deliberate pause. A missing DB snapshot cannot
claim ready. This status
projection does not change the business API's pause middleware. The responding
ops process SHA and source checkout SHA remain read-only status metadata;
these fields do not certify every sibling daemon's running code.

`ops/cluster_status/schema_mismatch.py` compares the applied migration set
with the running image's required set. Wheel runtimes use installed SQL metadata without Git.
The status contract contains the diagnosis kind, machine and detail; it has no
Git-pin category, watchdog counters, held-service projection, or stranded-hold
record. Ordinary pause and maintenance status remain independent of this
read-only schema diagnosis. Invalid database catalogs or image migration layouts
report `invalid-migration-layout`; query and connection failures report
`unavailable`. Only a successful comparison of equal sets returns no diagnosis.
The batched host snapshot also reports an unavailable comparison when its shared
database read fails, without retrying through another connection.
