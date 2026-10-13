# Ops module

The ops layer owns desired service state, lifecycle operations and deployment
coordination. Its design rationale is recorded in
[the ops decision](../../docs/decisions/runtime/hosts/2026-07-19-ops-k8s-semantics-without-k8s.md).

| Responsibility | Current implementation |
|---|---|
| Service specification | not in `ops`: the service roster, its `ServiceSpec` contract and gating live in `services/supervision/roster/` (the supervised services own their own declarations) |
| Observation and status | `ops/cluster_status/` (host snapshot, `pause.py` posture, `schema_mismatch` diagnosis) |
| Native agent drain | `ops/agent_pause/` (drain, `probe` of the running host) |
| Agent lifecycle | `ops/agents/` (birth and wake); `ops/lifecycle/` (lifecycle RPC ops) |
| RPC | `ops/rpc_schemas/` (wire vocabulary); `ops/cluster/rpc.py` (gateway client) |
| Op clusters the ops server dispatches | `ops/lifecycle/`, `ops/cluster/operations.py`, `ops/host_config.py`, `ops/inventory.py` |
| Stop and restart | CLI maintenance orchestration over the shared drain |
| Fleet update | `cli/fleet_update.py` (down and up scripts per unit, gated by the code version) |

Each package door is the module it grew from; `ops.agents`,
`ops.rpc_schemas`, `ops.cluster.operations_status` and `ops.agent_pause`
kept their import paths. Module names never repeat the package name, so the
op clusters read `ops.lifecycle`, `ops.cluster.operations.operations`, `ops.host_config`
and `ops.inventory`. `ops/private_files/` owns the private-file
verifier and its manifest.
Its operator entry point remains `python -m ops.private_files`.

`build_services()` supplies the application root manifest and local status roster.
A plugin service may bind a frozen, non-secret `plugin_config` snapshot to its
`ServiceSpec` and gate. The root launcher serializes that instance into the unit
environment; the existing manifest digest includes it. Daemon boot validates the
complete packet against its declared schema instead of rereading mutable disk.
Malformed, mismatched or incomplete packets fail; absent packets identify a
direct development invocation. Cluster bootstrap cannot replace this service
birth carrier. Authoritative files remain in `config_inputs` when present.
The [checked service inventory](../../services/supervision/roster/docs/service-roster.md) lives beside the roster;
its sentinel table is validated against that registration in both directions.
Agent-runner units execute agents inside one agent host. There is no per-agent
process launcher or restarter service. Native service sessions and the
`pty-sessions` service's persistent shells remain separate execution resources.

The application root owns service supervision. There is no controller manager,
background checkout/update trigger, scheduled updater reaper, or automatic
stranded-hold restart path.

Stop, restart and update hold admission and wait for native restart, checkpoint
flush, actual continuation completion and resource settlement. Ordinary stop
shares that drain and then closes the selected local services, PTYs and data
plane; restart keeps the data plane and browser.
Timeout fails without implicit force. The complete operator contract is in
[graceful maintenance](../../docs/conventions/operations/graceful-maintenance.md).

A fleet update is `python -m cli.fleet_update`. What the update path still lacks
is recorded in the
[unified lifecycle plan](../../future/infra/lifecycle/unified-cluster-lifecycle.md).

The native OS unit supervises the application root, which owns its service
subprocesses. Agent shells are held by the `pty-sessions` service. Stop verifies captured
process identity before signalling.

The `ops.pty_close_notices` one-shot child reads its staged batch before
constructing any database handle or capturing its source image. A nonempty batch
uses one captured image and a nonexempt process database gate; an unknown image
stays unknown rather than using the checkout's current HEAD. The child retains
failed notices for the next start and clears only a successfully written batch.

The import boundary is `base < ops < {gateway, cli}`. The supported RPC
vocabulary lives in the `ops/rpc_schemas/` door; focused agent contracts live in
its `terminate` and `content` submodules. Gateway-only
schemas stay in `gateway/schemas/`.
The client and daemon reject unknown kinds before machine lookup, maintenance
admission, dedupe, or dispatch. Callers import live pause, status and recovery
definitions directly. The maintenance fences enforce admission.
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
record. Ordinary stop and the hold status remain independent of this
read-only schema diagnosis. Invalid database catalogs or image migration layouts
report `invalid-migration-layout`; query and connection failures report
`unavailable`. Only a successful comparison of equal sets returns no diagnosis.
The batched host snapshot also reports an unavailable comparison when its shared
database read fails, without retrying through another connection.

## Birth transaction ownership

`ops.agents.spawn.create_agent_row` retains its public transaction and
post-commit announcement contract. Its cursor-owned SQL writer is
`ops.agents.birth_transaction.insert_agent_birth`; see
[creation transaction primitives](../../base/agents/tasks/docs/creation-transactions.ava.okf.md).
The writer opens no connection, commits nothing and performs no launch.
A keyed birth retains its original snapshot in this same transaction; see [[gateway/agents/docs/guarded-drafts.ava.okf.md]].
