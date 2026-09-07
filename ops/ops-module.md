# Ops module

The ops layer owns desired service state, lifecycle operations and deployment
coordination. Its design rationale is recorded in
[the ops decision](../decisions/2026-07-19-ops-k8s-semantics-without-k8s.md).

| Responsibility | Current implementation |
|---|---|
| Service specification | `ops/service_spec.py`, `ops/roster.py`, `ops/spec.py` |
| Observation and status | `ops/observe.py`, `ops/cluster_status.py` |
| Controller ordering | `ops/manager.py` and `ops/controllers/` |
| Native agent drain | `ops/agent_pause.py` and `ops/agent_pause_probe.py` |
| Pause, stop and restart | CLI maintenance orchestration over the shared drain |
| Cluster rollout | `ava cluster update`, gateway barrier and runner self-updates |

`build_services()` is the source for local startup and watchdog healthchecks.
Agent-runner units execute agents inside one agent host. There is no per-agent
process launcher or restarter service. Native service sessions and persistent
PTY hosts remain separate execution resources.

The watchdog reconciles updater/rollout ownership, pause, schema and pinned
code before attempting service recovery. A blocking result carries a scope;
the watchdog maps it against each service's dependency flags. Controllers do
not maintain their own service lists.

Pause and update hold admission and wait for native restart, checkpoint flush,
actual continuation completion and resource settlement. Ordinary stop shares
that drain and then closes the selected local services, PTYs and data plane.
Timeout fails without implicit force. The complete operator contract is in
[graceful maintenance](../conventions/graceful-maintenance.md).

Gateway rollout drains all participating runners before schema migration.
Readiness is necessary for resumption but cannot replace a missing drain or
checkpoint receipt. The installed version's first update must respect its
actual protocol capabilities; see the maintenance runbook.

Service sessions use the platform's native supervisor (`shared/posixproc.py`
or Windows winproc). Agent shells use independent PTY hosts. Healthchecks and
CLI launch paths share the session backend; stop verifies captured process
identity before signalling. No K8s runtime or image deployment is involved.

The import boundary is `shared < ops < {gateway, cli}`. Shared RPC contracts
stay in `ops/rpc_schemas.py`; gateway-only schemas stay in `gateway/schemas/`.
Cluster identity remains the installed home path, resolved before runtime
configuration construction.
