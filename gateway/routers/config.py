"""Runtime config panel — /api/config GET/PUT.

Reads a machine's host fields (this gateway's own, or a remote online
agent-runner's via its ops server) plus the gateway's own
machine-independent cluster/agent fields, and writes edits back scope-routed
into `.env` (cluster -> the gateway's `.env`, host -> the target machine's own
`.env`). Field metadata + masking happens here.

`?machine=` selects the host whose host-scope fields are read/written:
- this gateway (a dual-role box) -> dispatched to its OWN ops server at its
  registered localhost URL, same as any runner — no local shortcut. A pure
  gateway (no agent-runner role) is read/written in-process: it runs no ops
  server to dial (the roster's structural exception).
- a remote agent-runner -> dispatch config_read / config_write via its ops
  server (404 unknown machine, 503 offline / timed out / no ops server).
Cluster + agent fields are always the gateway's own (machine-independent),
so a remote PUT may carry host-scope fields only.

`GET /api/config/resolved?model=` is the read-only per-model companion: it
resolves the per-model-defaultable subset (`shared/lm/registry.py:ModelTuning`)
for one model and names the layer each effective value came from. It adds no
write path — an explicit value is still edited as the ordinary config field of
the same name, through the PUT above.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException

from gateway.schemas import (
    ConfigFieldView,
    ConfigFieldWriteResult,
    ConfigView,
    ConfigWriteResult,
    ResolvedConfigView,
    ResolvedFieldView,
)
from ops import cluster_rpc as _cluster_rpc
from ops import ops_config
from ops.ops_config import SENSITIVE_MASK
from ops.rpc_schemas import ConfigReadResult, ConfigWriteOpResult
from shared import runtime_config
from shared.config import env_override_values, field_domain, get_config_metadata, settings
from shared.config.candidate import validate_env_patch_for_write
from shared.config.editing import ConfigPatchPlan, split_reducer_patch
from shared.env_audit import check_env_integrity
from shared.machine import MachineRole, machine_name

router = APIRouter()


def _assert_machine_known(target: str) -> None:
    """404 if `target` is not in the machines table — distinguishes a typo'd
    machine name from a registered-but-offline host (which 503s on timeout).

    Skips the lookup for `target == machine_name()` (this host always exists,
    even before its own startup UPSERT lands).
    """
    if target == machine_name():
        return
    from gateway.app import app

    with app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM machines WHERE name = %s", (target,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"unknown machine: {target!r}")


def _target_capabilities(target: str) -> list[MachineRole]:
    """The target machine's capability set (`gateway` / `agent-runner` /
    `observability-station`) — what the panel uses to pick which capability
    sections a remote view renders.

    For this gateway itself (`target == machine_name()`) the local `machine_role()`
    is authoritative — it is always available, even before the host's own startup
    UPSERT lands. For a remote host it is the registered `machines.role`; an
    unregistered name resolves to an empty set (the read still works, the panel just
    shows the common section).
    """
    if target == machine_name():
        from shared.machine import machine_role

        return cast("list[MachineRole]", sorted(machine_role()))
    from shared.machines import MachineNotRegistered, lookup_role

    try:
        # machines.role is written from the capability tokens, so it is a
        # gateway/agent-runner list; the DB read is typed str.
        return cast("list[MachineRole]", lookup_role(target))
    except MachineNotRegistered:
        return []


async def _dispatch_config_read(target: str) -> ConfigReadResult:
    """Run config_read on `target` via its ops server — one uniform path.

    Every machine with an agent-runner capability (the gateway's own box
    included, dialed at its registered localhost URL) is read through its ops
    server; an unreachable or failed op 503s. The only in-process read is the
    gateway's OWN config when the local host has no agent-runner role (a pure
    gateway / not-yet-registered host runs no ops server to dial — the same
    structural exception as the roster's lightweight local row). Caller has
    already verified the machine is known.
    """
    from shared.machines import MachineNotRegistered, lookup_role

    try:
        role = await asyncio.to_thread(lookup_role, target)
    except MachineNotRegistered:
        role = []
    if "agent-runner" in role:
        try:
            wire = await _cluster_rpc.dispatch_to_machine(
                target_machine=target,
                kind="config_read",
                payload={},
            )
        except _cluster_rpc.ClusterOpUnreachable:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} ops server unreachable for config read",
            ) from None
        except _cluster_rpc.ClusterOpFailed as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} config read failed: {exc.result!r}",
            ) from exc
        return ConfigReadResult.model_validate(wire)
    # No agent-runner role. Only the gateway's own box may be read in-process —
    # a pure-gateway / not-yet-registered local host runs no ops server to dial
    # (the same structural exception as the roster's lightweight local row). A
    # REMOTE machine without agent-runner is unreachable by construction: never
    # silently answer it with THIS host's config.
    if target != machine_name():
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} has no agent-runner ops server — its config cannot be read",
        )
    return ConfigReadResult.model_validate(await asyncio.to_thread(ops_config.config_read_op))


async def _dispatch_config_write(
    target: str, overrides: dict[str, Any], *, local: bool = False
) -> ConfigWriteOpResult:
    """Run config_write on `target` via its ops server — one uniform path.

    Every machine with an agent-runner capability (the gateway's own box
    included, dialed at its registered localhost URL) is written through its
    ops server; an unreachable or failed op 503s. The only in-process write is
    the gateway's OWN config when the local host has no agent-runner role (a
    pure gateway / not-yet-registered host runs no ops server to dial). A
    remote machine without agent-runner 503s — never silently redirected onto
    this host's .env. `local` propagates through the dispatch so the
    target-side op applies the correct writability gate (writable vs
    remote_writable). Caller has already verified the machine is known.
    """
    from shared.machines import MachineNotRegistered, lookup_role

    try:
        role = await asyncio.to_thread(lookup_role, target)
    except MachineNotRegistered:
        role = []
    if "agent-runner" in role:
        try:
            wire = await _cluster_rpc.dispatch_to_machine(
                target_machine=target,
                kind="config_write",
                payload={"overrides": overrides, "local": local},
            )
        except _cluster_rpc.ClusterOpUnreachable:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} ops server unreachable for config write",
            ) from None
        except _cluster_rpc.ClusterOpFailed as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} config write failed: {exc.result!r}",
            ) from exc
        return ConfigWriteOpResult.model_validate(wire)
    # No agent-runner role: only the gateway's own box (pure-gateway /
    # not-yet-registered local host) may be written in-process — it runs no ops
    # server. A remote machine without agent-runner is unreachable by
    # construction and must never be silently redirected onto THIS host's .env.
    if target != machine_name():
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} has no agent-runner ops server — its config cannot be written",
        )
    return ConfigWriteOpResult.model_validate(
        await asyncio.to_thread(ops_config.config_write_op, overrides, local=local)
    )


def _assemble_write_result(
    host_result: ConfigWriteOpResult, restart_required: list[str]
) -> ConfigWriteResult:
    """Project a config_write outcome into the wire ConfigWriteResult — the one
    place the per-field results + restart union are shaped, shared by the local
    and remote PUT branches (they differ only in how restart_required is
    computed)."""
    return ConfigWriteResult(
        applied=host_result.applied,
        results={
            f: ConfigFieldWriteResult(ok=r.ok, reason=r.reason)
            for f, r in host_result.results.items()
        },
        restart_required=restart_required,
    )


@router.get("/api/config")
async def get_config(machine: str | None = None) -> ConfigView:
    """Return metadata + current value + the override set for all config items.

    Host-scope fields come from `machine` (default = this gateway): their
    effective value + read-time capability hint are produced by a config_read on
    that host. Cluster + agent fields are always the gateway's own
    (machine-independent), masked here. `raw_overrides` is the editable set the
    panel deltas against, and it is keyed on whether `machine` was GIVEN, not on
    whether the target is this gateway: the Cluster view (machine omitted) deltas
    against this gateway's manageable cluster + writable-host .env set; a
    machine-addressed view (`?machine=<name>`, even == this gateway on a dual-role
    box) deltas against that host's remotely-writable host set.
    """
    await asyncio.to_thread(check_env_integrity)
    target = machine or machine_name()
    await asyncio.to_thread(_assert_machine_known, target)
    read = await _dispatch_config_read(target)
    host_fields = read.host_fields

    fields: list[ConfigFieldView] = []
    for meta in get_config_metadata():
        if meta.scope == "host":
            hf = host_fields[meta.name]
            fields.append(
                ConfigFieldView(
                    name=meta.name,
                    field_type=meta.field_type,
                    current_value=hf.value,
                    default_value=meta.default_value,
                    description=meta.description,
                    group=meta.group,
                    capability=meta.capability,
                    restart_required=meta.restart_required,
                    writable=meta.writable,
                    sensitive=meta.sensitive,
                    env_var=meta.env_var,
                    scope=meta.scope,
                    remote_writable=meta.remote_writable,
                    per_agent=meta.per_agent,
                    choices=meta.choices,
                    can_enable=hf.can_enable,
                    reason=hf.reason,
                )
            )
        else:
            # Cluster / agent fields are machine-independent — the gateway's
            # own current value (masked if sensitive); no per-host capability hint.
            value = meta.current_value
            if meta.sensitive and value:
                value = SENSITIVE_MASK
            fields.append(
                ConfigFieldView(
                    name=meta.name,
                    field_type=meta.field_type,
                    current_value=value,
                    default_value=meta.default_value,
                    description=meta.description,
                    group=meta.group,
                    capability=meta.capability,
                    restart_required=meta.restart_required,
                    writable=meta.writable,
                    sensitive=meta.sensitive,
                    env_var=meta.env_var,
                    scope=meta.scope,
                    remote_writable=meta.remote_writable,
                    per_agent=meta.per_agent,
                    choices=meta.choices,
                    can_enable=None,
                    reason=None,
                )
            )

    # Semantics key on whether `machine` was given, NOT on target == this gateway:
    # the Cluster view (machine omitted) edits cluster fields + this gateway's own
    # host fields via `writable`, so it deltas against the manageable set. A
    # machine-addressed view (`?machine=<name>`, even == this gateway on a
    # dual-role box) edits only that host's remotely-writable host fields, so it
    # deltas against the host override set. Without this split an explicit self
    # selection would collapse into the Cluster view and its host-only,
    # remote_writable-gated fields (heartbeat_enabled / task_maintenance_enabled)
    # would be unreachable.
    raw_overrides = env_override_values(local=True) if machine is None else read.raw_overrides

    return ConfigView(
        fields=fields,
        raw_overrides=raw_overrides,
        machine_capabilities=_target_capabilities(target),
    )


@router.get("/api/config/resolved")
def get_resolved_config(model: str | None = None) -> ResolvedConfigView:
    """Resolve every per-model-defaultable setting for one model — read-only.

    Answers "what will an agent on this model actually run with, and which layer
    decided that": shared default < per-model default (both code, in
    `shared/lm/registry.py`) < explicit `.env` value. `explain_setting` does the
    layering — the same function the runtime resolves through, so this view
    cannot drift from the value an agent gets.

    The explicit layer is each field's `current_value` from the config metadata,
    i.e. read fresh from `.env` rather than the gateway's boot-time settings —
    matching what the config panel shows and what the next agent process boots
    with (every tunable here is `restart_required="agent"`).

    Machine-independent by construction: every tunable is cluster-scope, so
    there is no `?machine=` here. The per-agent overlay — the one layer above
    explicit — is per process and invisible from the cluster; `per_agent` marks
    which fields a spawn/restart overlay may still override.
    """
    from shared.config import per_agent_field_names
    from shared.lm.factory import ensure_provider_plugins_loaded
    from shared.lm.registry import MODELS, explain_setting, tuning_field_names

    # Plugin models must be registered before the registry lookup below.
    ensure_provider_plugins_loaded()

    target = model or settings.lm.llm_model
    metas = {m.name: m for m in get_config_metadata()}
    per_agent = per_agent_field_names()

    fields: list[ResolvedFieldView] = []
    for name in tuning_field_names():
        # A ModelTuning field name is a config field name by invariant
        # (tests/shared/test_model_registry.py) — hard index, so a rename that
        # orphans one side 500s here instead of silently dropping the row.
        meta = metas[name]
        resolved = explain_setting(name, model=target, explicit=meta.current_value)
        fields.append(
            ResolvedFieldView(
                name=name,
                env_var=meta.env_var,
                description=meta.description,
                field_type=meta.field_type,
                choices=meta.choices,
                group=meta.group,
                effective_value=resolved.value,
                source=cast(
                    "Literal['explicit', 'model-default', 'shared-default']", resolved.source
                ),
                explicit_value=resolved.explicit_value,
                model_default=resolved.model_default,
                shared_default=resolved.shared_default,
                per_agent=name in per_agent,
                restart_required=meta.restart_required,
            )
        )

    return ResolvedConfigView(model=target, registered=target in MODELS, fields=fields)


@router.put("/api/config")
async def put_config(body: dict[str, object], machine: str | None = None) -> ConfigWriteResult:
    """Merge a config patch for `machine` (default = this gateway) into `.env`,
    scope-routed. Persist only — no restart (restart_required says which process
    to restart).

    The body is parsed by `ConfigPatchPlan.parse` (shared with the host-side
    config_write_op): the editability gate, scope routing, the merge-patch
    reducer and cluster-scalar coercion all live there as one pure function.
    Reducer / JSON-merge-patch semantics: a key with a value is set/replaced, a
    key mapped to `null` is unset (reverted to its default), and an ABSENT key is
    left untouched. Deletion is always the explicit null — never inferred from a
    key's absence — so a partial PUT can never drop a field it did not name (a
    full-replace once wiped a cluster's secrets that way). Only editable fields
    are accepted; the body is split by scope:
    - cluster-scope keys merge into the gateway's `.env` (machine-independent).
    - host-scope keys merge into the target machine's own `.env`, gated host-side
      by remote_writable + the capability validator.
    Agent-scope fields have writable=False and are rejected by the writable gate.

    A machine-addressed PUT (`?machine=<name>`, including this gateway's own name on
    a dual-role box) may carry host-scope fields only — a cluster key in the body
    400s ("edit it on the Cluster view"). A machine-addressed PUT also edits that
    host's fields via the remote_writable gate, EVEN when the name is this gateway
    itself on a dual-role box — keyed on whether `machine` was given, not on
    target == this gateway (otherwise an explicit self selection would collapse
    into the Cluster view and its host-only, remote_writable-gated toggles
    (heartbeat_enabled / task_maintenance_enabled — writable=False) would be
    rejected as read-only). The host-side op is authoritative for per-field host
    verdicts; the returned ConfigWriteResult surfaces them so the frontend can
    show inline failures + the per-machine restart banner.
    """
    target = machine or machine_name()
    await asyncio.to_thread(_assert_machine_known, target)

    metas = {m.name: m for m in get_config_metadata()}
    plan = ConfigPatchPlan.parse(body, metas, is_remote=machine is not None)
    if plan.violations:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or read-only fields: {sorted(plan.violations)}",
        )
    if plan.scalar_error:
        raise HTTPException(status_code=400, detail=plan.scalar_error)
    if plan.remote_cluster_keys:
        raise HTTPException(
            status_code=400,
            detail="cluster config is machine-independent; edit it on the Cluster view",
        )
    has_cluster_patch = bool(plan.cluster_writes or plan.cluster_removals)
    if has_cluster_patch:
        candidate_writes = dict(plan.cluster_writes)
        candidate_removals = set(plan.cluster_removals)
        # A local host field shares the gateway's `.env`. Include only host edits
        # from a cluster-touched domain: this catches a cross-scope PITR transition
        # before its host write, without changing the existing host capability-result
        # contract for unrelated fields.
        cluster_domains = {
            field_domain(name) for name in set(candidate_writes) | candidate_removals
        }
        if machine is None and cluster_domains:
            host_writes, host_removals = split_reducer_patch(plan.host_body, metas)
            for name, value in host_writes.items():
                if field_domain(name) in cluster_domains:
                    candidate_writes[name] = value
            candidate_removals.update(
                name for name in host_removals if field_domain(name) in cluster_domains
            )
        candidate = validate_env_patch_for_write(candidate_writes, candidate_removals)
        if candidate.errors:
            raise HTTPException(
                status_code=400,
                detail="candidate config rejected: " + "; ".join(candidate.errors),
            )

    if machine is None:
        # Host first: if a host field is rejected the write returns applied=False
        # and we leave the cluster .env untouched (atomic from the cluster's view).
        # No in-memory apply, no restart — the change is persisted and the named
        # process picks it up on its next restart (restart_required says which).
        host_result = await _dispatch_config_write(target, plan.host_body, local=True)
        cluster_changed: set[str] = set()
        if host_result.applied and has_cluster_patch:
            # A successful host write can legitimately change this same local
            # file. Revalidate the cluster patch against that new image and use
            # its digest as the compare-and-swap precondition for persistence.
            candidate = validate_env_patch_for_write(plan.cluster_writes, plan.cluster_removals)
            if candidate.errors:
                raise HTTPException(
                    status_code=400,
                    detail="candidate config rejected: " + "; ".join(candidate.errors),
                )
            try:
                await asyncio.to_thread(
                    runtime_config.write_fields,
                    plan.cluster_writes,
                    plan.cluster_removals,
                    expected_digest=candidate.expected_digest,
                    audit_site="gateway_config_put",
                )
            except RuntimeError as exc:
                if str(exc) != ".env changed before owned runtime-config write":
                    raise
                raise HTTPException(
                    status_code=409,
                    detail="config changed concurrently; retry the request",
                ) from None
            cluster_changed = set(plan.cluster_writes) | plan.cluster_removals
        cluster_restart = {
            metas[k].restart_required for k in cluster_changed if metas[k].restart_required
        }
        restart_required = sorted(cluster_restart | set(host_result.restart_required))
    else:
        # Remote target: cluster config is machine-independent; only host fields
        # go. Invariant: an agent-runner's host .env set is written only by
        # config_write_op (remote_writable host keys), so a remote raw_overrides
        # never carries a cluster/agent key — the remote_cluster_keys 400 guards a
        # hand-edited .env, not normal traffic. The host-side op stays the
        # authoritative per-field gate.
        host_result = await _dispatch_config_write(target, plan.host_body)
        restart_required = host_result.restart_required

    return _assemble_write_result(host_result, restart_required)
