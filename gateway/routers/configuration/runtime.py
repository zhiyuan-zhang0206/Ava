from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg_pool import ConnectionPool

from base.api_contracts.config import (
    ConfigAuditView,
    ConfigFieldView,
    ConfigFieldWriteResult,
    ConfigView,
    ConfigWriteResult,
    ResolvedConfigView,
    ResolvedFieldView,
)
from base.cluster.machine import MachineIdentity, MachineRole
from base.config import field_domain, runtime_config, settings
from base.config.admin.candidate import validate_env_patch_for_write
from base.config.admin.editing import ConfigPatchPlan, split_reducer_patch
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.hostport import Host
from base.packages.plugin_config_admin import (
    panel_metadata,
    panel_overrides,
    patch_owner,
    write_plugin_patch,
)
from base.packages.plugin_config_images import PluginConfigChangedError, PluginConfigOwner
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from gateway.http.model_catalog import require_model_catalog
from ops import host_config
from ops.cluster import rpc as cluster_rpc
from ops.env_audit import EnvWriteAudit, check_env_integrity
from ops.host_config import SENSITIVE_MASK
from ops.rpc_schemas import ConfigAuditReadResult, ConfigReadResult, ConfigWriteOpResult

router = APIRouter()


def request_actor(request: Request) -> tuple[str | None, str | None]:

    verified_by = getattr(request.state, "source_verified_by", None)
    principal = getattr(request.state, "auth_principal", None)
    actor: str | None = None
    if isinstance(verified_by, str) and verified_by:
        subject = getattr(principal, "subject", None)
        actor = f"{verified_by}:{subject}" if isinstance(subject, str) and subject else verified_by
    trace_id = getattr(request.state, "trace_id", None)
    return actor, trace_id if isinstance(trace_id, str) else None


def _assert_machine_known(target: str, pool: ConnectionPool, *, local_machine_name: str) -> None:

    if target == local_machine_name:
        return

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM machines WHERE name = %s", (target,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"unknown machine: {target!r}")


def _target_capabilities(
    target: str, db: Database, *, local_machine: MachineIdentity
) -> list[MachineRole]:

    if target == local_machine.name():
        return cast("list[MachineRole]", sorted(local_machine.role()))
    from base.cluster.machines import MachineNotRegistered, lookup_role

    try:
        return cast("list[MachineRole]", lookup_role(db, target))
    except MachineNotRegistered:
        return []


async def _dispatch_config_read(
    target: str,
    *,
    db: Database,
    authority: ConfigAuthority,
    local_machine: MachineIdentity,
    host: Host,
) -> ConfigReadResult:

    from base.cluster.machines import MachineNotRegistered, lookup_role

    try:
        role = await asyncio.to_thread(lookup_role, db, target)
    except MachineNotRegistered:
        role = []
    if "agent-runner" in role:
        try:
            wire = await cluster_rpc.dispatch_to_machine(
                db,
                target_machine=target,
                kind="config_read",
                payload={},
            )
        except cluster_rpc.ClusterOpUnreachable:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} ops server unreachable for config read",
            ) from None
        except cluster_rpc.ClusterOpFailed as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} config read failed: {exc.result!r}",
            ) from exc
        return ConfigReadResult.model_validate(wire)

    if target != local_machine.name():
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} has no agent-runner ops server — its config cannot be read",
        )
    return ConfigReadResult.model_validate(
        await asyncio.to_thread(
            host_config.config_read_op,
            authority=authority,
            local_machine=local_machine.name(),
            host=host,
        )
    )


async def _dispatch_config_audit_read(
    target: str, last: int, *, db: Database, local_machine: MachineIdentity
) -> ConfigAuditReadResult:

    from base.cluster.machines import MachineNotRegistered, lookup_role

    try:
        role = await asyncio.to_thread(lookup_role, db, target)
    except MachineNotRegistered:
        role = []
    if "agent-runner" in role:
        try:
            wire = await cluster_rpc.dispatch_to_machine(
                db,
                target_machine=target,
                kind="config_audit_read",
                payload={"last": last},
            )
        except cluster_rpc.ClusterOpUnreachable:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} ops server unreachable for config audit read",
            ) from None
        except cluster_rpc.ClusterOpFailed as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} config audit read failed: {exc.result!r}",
            ) from exc
        return ConfigAuditReadResult.model_validate(wire)
    if target != local_machine.name():
        raise HTTPException(
            status_code=503,
            detail=(
                f"machine {target!r} has no agent-runner ops server — "
                "its config audit cannot be read"
            ),
        )
    return ConfigAuditReadResult.model_validate(
        await asyncio.to_thread(
            host_config.config_audit_read_op, last, local_machine=local_machine.name()
        )
    )


async def _dispatch_config_write(
    target: str,
    overrides: dict[str, Any],
    *,
    db: Database,
    authority: ConfigAuthority,
    local: bool = False,
    actor: str | None = None,
    trace_id: str | None = None,
    local_machine: MachineIdentity,
    host: Host,
) -> ConfigWriteOpResult:

    from base.cluster.machines import MachineNotRegistered, lookup_role

    try:
        role = await asyncio.to_thread(lookup_role, db, target)
    except MachineNotRegistered:
        role = []
    if "agent-runner" in role:
        try:
            wire = await cluster_rpc.dispatch_to_machine(
                db,
                target_machine=target,
                kind="config_write",
                payload={
                    "overrides": overrides,
                    "local": local,
                    "actor": actor,
                    "trace_id": trace_id,
                },
            )
        except cluster_rpc.ClusterOpUnreachable:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} ops server unreachable for config write",
            ) from None
        except cluster_rpc.ClusterOpFailed as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target!r} config write failed: {exc.result!r}",
            ) from exc
        return ConfigWriteOpResult.model_validate(wire)

    if target != local_machine.name():
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} has no agent-runner ops server — its config cannot be written",
        )
    return ConfigWriteOpResult.model_validate(
        await asyncio.to_thread(
            host_config.config_write_op,
            overrides,
            authority=authority,
            local=local,
            actor=actor,
            trace_id=trace_id,
            local_machine=local_machine.name(),
            host=host,
        )
    )


def _assemble_write_result(
    host_result: ConfigWriteOpResult, restart_required: list[str]
) -> ConfigWriteResult:

    return ConfigWriteResult(
        applied=host_result.applied,
        results={
            f: ConfigFieldWriteResult(ok=r.ok, reason=r.reason)
            for f, r in host_result.results.items()
        },
        restart_required=restart_required,
    )


@router.get(
    "/api/config",
    description="Return metadata + current value + the override set for all config items.\n\nHost-scope fields come from `machine` (default = this gateway): their\neffective value + read-time capability hint are produced by a config_read on\nthat host. Cluster + agent fields are always the gateway's own\n(machine-independent), masked here. `raw_overrides` is the editable set the\npanel deltas against, and it is keyed on whether `machine` was GIVEN, not on\nwhether the target is this gateway: the Cluster view (machine omitted) deltas\nagainst this gateway's manageable cluster + writable-host .env set; a\nmachine-addressed view (`?machine=<name>`, even == this gateway on a dual-role\nbox) deltas against that host's remotely-writable host set.",
)
async def get_config(request: Request, machine: str | None = None) -> ConfigView:

    await asyncio.to_thread(check_env_integrity, machine_reader=request.app.state.machine.name)
    target = machine or request.app.state.machine.name()
    await asyncio.to_thread(
        _assert_machine_known,
        target,
        request.app.state.db_pool,
        local_machine_name=request.app.state.machine.name(),
    )
    read = await _dispatch_config_read(
        target,
        db=request.app.state.db,
        authority=request.app.state.config_authority,
        local_machine=request.app.state.machine,
        host=request.app.state.host,
    )
    host_fields = read.host_fields

    fields: list[ConfigFieldView] = []
    for meta in panel_metadata(authority=request.app.state.config_authority):
        if meta.scope == "host":
            hf = host_fields[meta.name]
            fields.append(
                ConfigFieldView(
                    name=meta.name,
                    owner=meta.owner,
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
            value = meta.current_value
            if meta.sensitive and value:
                value = SENSITIVE_MASK
            fields.append(
                ConfigFieldView(
                    name=meta.name,
                    owner=meta.owner,
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

    raw_overrides = (
        panel_overrides(authority=request.app.state.config_authority, local=True)
        if machine is None
        else read.raw_overrides
    )

    return ConfigView(
        fields=fields,
        raw_overrides=raw_overrides,
        machine_capabilities=_target_capabilities(
            target, request.app.state.db, local_machine=request.app.state.machine
        ),
    )


@router.get(
    "/api/config/audit",
    description="Return the `.env` write audit trail, newest first.\n\n`machine` selects the source: omitted = this gateway's own box; `all` = every\nagent-runner machine plus the gateway's own box, merged (fail-fast — an\nunreachable machine 503s the whole read); a machine name = that machine.\nOmitted `last` returns the configured default count\n(``display.config_audit_default_last`` - 20 out of the box); an explicit\n`last` stays capped at 200. Records are the raw\naudit-JSONL entries (`ops/env_audit.py`), each tagged with its `machine`;\nvalues were redacted at write time (non-sensitive fields only), and records\nfrom before record v2 lack `actor` / `trace_id` / `changed`.",
)
async def get_config_audit(
    request: Request,
    machine: str | None = None,
    last: int | None = Query(default=None, ge=1, le=200),
) -> ConfigAuditView:

    effective_last = last if last is not None else settings.display.config_audit_default_last
    if machine == "all":
        from base.cluster.machines import list_agent_runners

        runners = await asyncio.to_thread(list_agent_runners, request.app.state.db)
        names = [name for name, _url in runners]
        if request.app.state.machine.name() not in names:
            names.append(request.app.state.machine.name())
        results = await asyncio.gather(
            *(
                _dispatch_config_audit_read(
                    name,
                    effective_last,
                    db=request.app.state.db,
                    local_machine=request.app.state.machine,
                )
                for name in names
            )
        )
    else:
        target = machine or request.app.state.machine.name()
        await asyncio.to_thread(
            _assert_machine_known,
            target,
            request.app.state.db_pool,
            local_machine_name=request.app.state.machine.name(),
        )
        results = [
            await _dispatch_config_audit_read(
                target,
                effective_last,
                db=request.app.state.db,
                local_machine=request.app.state.machine,
            )
        ]

    records: list[dict[str, object]] = []
    for result in results:
        records.extend({**record, "machine": result.machine} for record in result.records)
    records.sort(key=lambda record: str(record.get("ts", "")), reverse=True)
    return ConfigAuditView(records=records[:effective_last])


@router.get(
    "/api/config/resolved",
    description='Resolve every per-model-defaultable setting for one model — read-only.\n\nAnswers "what will an agent on this model actually run with, and which layer\ndecided that": shared default < per-model default (both code, in\n`base/lm/registry.py`) < explicit `.env` value. `explain_setting` does the\nlayering — the same function the runtime resolves through, so this view\ncannot drift from the value an agent gets.\n\nThe explicit layer is each field\'s `current_value` from the config metadata,\ni.e. read fresh from `.env` rather than the gateway\'s boot-time settings —\nmatching what the config panel shows and what the next agent process boots\nwith (every tunable here is `restart_required="agent"`).\n\nMachine-independent by construction: every tunable is cluster-scope, so\nthere is no `?machine=` here. The per-agent overlay — the one layer above\nexplicit — is per process and invisible from the cluster; `per_agent` marks\nwhich fields a spawn/restart overlay may still override.',
)
def get_resolved_config(request: Request, model: str | None = None) -> ResolvedConfigView:

    from base.config import per_agent_field_names
    from base.lm.registry import explain_setting, tuning_field_names

    catalog = require_model_catalog(request)
    target = model or settings.lm.llm_model
    metas = {m.name: m for m in panel_metadata(authority=request.app.state.config_authority)}
    per_agent = per_agent_field_names()

    fields: list[ResolvedFieldView] = []
    for name in tuning_field_names():
        meta = metas[name]
        resolved = explain_setting(
            name, model=target, explicit=meta.current_value, models=catalog.models
        )
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

    return ResolvedConfigView(model=target, registered=target in catalog.models, fields=fields)


def _reject_invalid_plan(plan: ConfigPatchPlan) -> None:
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


def _validate_cluster_candidate(
    plan: ConfigPatchPlan, metas: dict[str, Any], *, local: bool
) -> None:
    candidate_writes = dict(plan.cluster_writes)
    candidate_removals = set(plan.cluster_removals)

    cluster_domains = {field_domain(name) for name in set(candidate_writes) | candidate_removals}
    if local and cluster_domains:
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


async def _put_local(
    target: str,
    plan: ConfigPatchPlan,
    metas: dict[str, Any],
    *,
    db: Database,
    authority: ConfigAuthority,
    has_cluster_patch: bool,
    actor: str | None,
    trace_id: str | None,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[ConfigWriteOpResult, list[str]]:

    host_result = await _dispatch_config_write(
        target,
        plan.host_body,
        db=db,
        authority=authority,
        local=True,
        actor=actor,
        trace_id=trace_id,
        local_machine=local_machine,
        host=host,
    )
    cluster_changed: set[str] = set()
    if host_result.applied and has_cluster_patch:
        await _write_cluster_fields(plan, actor=actor, trace_id=trace_id)
        cluster_changed = set(plan.cluster_writes) | plan.cluster_removals
    cluster_restart = {
        metas[k].restart_required for k in cluster_changed if metas[k].restart_required
    }
    return host_result, sorted(cluster_restart | set(host_result.restart_required))


async def _write_cluster_fields(
    plan: ConfigPatchPlan, *, actor: str | None, trace_id: str | None
) -> None:

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
            audit=EnvWriteAudit(site="gateway_config_put", actor=actor, trace_id=trace_id),
        )
    except RuntimeError as exc:
        if str(exc) != ".env changed before owned runtime-config write":
            raise
        raise HTTPException(
            status_code=409,
            detail="config changed concurrently; retry the request",
        ) from None


async def _put_plugin(
    owner: PluginConfigOwner, plan: ConfigPatchPlan, metas: dict[str, Any]
) -> ConfigWriteResult:

    writes, removals = split_reducer_patch(plan.host_body, metas)
    writes.update(plan.cluster_writes)
    removals.update(plan.cluster_removals)
    try:
        await asyncio.to_thread(write_plugin_patch, owner, writes, removals, expected_digest=None)
    except InvalidConfigOverlay as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PluginConfigChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    restart = sorted(
        {metas[k].restart_required for k in set(writes) | removals if metas[k].restart_required}
    )
    return ConfigWriteResult(
        results={k: ConfigFieldWriteResult(ok=True, reason=None) for k in set(writes) | removals},
        applied=True,
        restart_required=restart,
    )


@router.put(
    "/api/config",
    description="Merge a config patch for `machine` (default = this gateway) into `.env`,\nscope-routed, with one declaration owner per request. Persist only — no restart (restart_required says which process\nto restart).\n\nPlugin fields commit one whole config image; mixed Core/plugin or multi-plugin\nbodies fail before any write. Core fields retain their env writer.\n\nThe body is parsed by `ConfigPatchPlan.parse` (shared with the host-side\nconfig_write_op): the editability gate, scope routing, the merge-patch\nreducer and cluster-scalar coercion all live there as one pure function.\nReducer / JSON-merge-patch semantics: a key with a value is set/replaced, a\nkey mapped to `null` is unset (reverted to its default), and an ABSENT key is\nleft untouched. Deletion is always the explicit null — never inferred from a\nkey's absence — so a partial PUT can never drop a field it did not name (a\nfull-replace once wiped a cluster's secrets that way). Only editable fields\nare accepted; the body is split by scope:\n- cluster-scope keys merge into the gateway's `.env` (machine-independent).\n- host-scope keys merge into the target machine's own `.env`, gated host-side\n  by remote_writable + the capability validator.\nAgent-scope fields have writable=False and are rejected by the writable gate.\n\nA machine-addressed PUT (`?machine=<name>`, including this gateway's own name on\na dual-role box) may carry host-scope fields only — a cluster key in the body\n400s (\"edit it on the Cluster view\"). A machine-addressed PUT also edits that\nhost's fields via the remote_writable gate, EVEN when the name is this gateway\nitself on a dual-role box — keyed on whether `machine` was given, not on\ntarget == this gateway (otherwise an explicit self selection would collapse\ninto the Cluster view and its host-only, remote_writable-gated toggles\n(heartbeat_enabled / task_maintenance_enabled — writable=False) would be\nrejected as read-only). The host-side op is authoritative for per-field host\nverdicts; the returned ConfigWriteResult surfaces them so the frontend can\nshow inline failures + the per-machine restart banner.",
)
async def put_config(
    request: Request, body: dict[str, object], machine: str | None = None
) -> ConfigWriteResult:

    target = machine or request.app.state.machine.name()
    actor, trace_id = request_actor(request)
    await asyncio.to_thread(
        _assert_machine_known,
        target,
        request.app.state.db_pool,
        local_machine_name=request.app.state.machine.name(),
    )

    metas = {m.name: m for m in panel_metadata(authority=request.app.state.config_authority)}
    plan = ConfigPatchPlan.parse(body, metas, is_remote=machine is not None)
    _reject_invalid_plan(plan)
    try:
        owner = patch_owner(set(body))
    except InvalidConfigOverlay as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if owner is not None and machine is None:
        return await _put_plugin(owner, plan, metas)
    has_cluster_patch = bool(plan.cluster_writes or plan.cluster_removals)
    if has_cluster_patch:
        _validate_cluster_candidate(plan, metas, local=machine is None)

    if machine is None:
        host_result, restart_required = await _put_local(
            target,
            plan,
            metas,
            db=request.app.state.db,
            authority=request.app.state.config_authority,
            has_cluster_patch=has_cluster_patch,
            actor=actor,
            trace_id=trace_id,
            local_machine=request.app.state.machine,
            host=request.app.state.host,
        )
    else:
        host_result = await _dispatch_config_write(
            target,
            plan.host_body,
            db=request.app.state.db,
            authority=request.app.state.config_authority,
            actor=actor,
            trace_id=trace_id,
            local_machine=request.app.state.machine,
            host=request.app.state.host,
        )
        restart_required = host_result.restart_required

    return _assemble_write_result(host_result, restart_required)
