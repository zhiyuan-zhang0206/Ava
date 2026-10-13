"""Recovery of a committed keyed agent creation before mutable spawn checks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import Header, HTTPException, Request
from psycopg_pool import ConnectionPool

from base.agents.labels import spawn_prompt_with_label
from base.agents.observation import snapshot as snapshot_module
from base.agents.observation.evidence import AgentAvailability, AvailabilityReason
from base.api_contracts.agent_contract import (
    AgentLaunchFailed,
    AgentNotFound,
    ForkConfigChangeNotAllowed,
    SpawnTargetNotAgentRunner,
)
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.log import logger
from gateway.agents import forward
from gateway.agents.forward import forward_spawn_to_remote
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    PrincipalScopeError,
    request_key,
)
from ops.agents.creation_identity import (
    CreationConflictError,
    CreationReceipt,
    creation_request_hash,
    find_creation,
)
from ops.agents.spawn import create_agent_row
from ops.lifecycle.launch import spawn_prechecks_blocking
from ops.rpc_schemas import ConfigNormalization, LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent


def scoped_creation_key(request: Request, key: str, *, operation_path: str) -> str:
    """Bind a caller key to the verified principal and the versioned operation path."""
    try:
        return request_key(request, key, method="POST", path=operation_path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def creation_receipt(pool: ConnectionPool, key: str, request_hash: str) -> CreationReceipt | None:
    try:
        with pool.connection() as conn:
            return find_creation(conn, key, request_hash)
    except CreationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def create_and_launch_agent(
    body: SpawnAgentRequest,
    target: str,
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    *,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    creation_key: str,
    creation_identity: dict[str, object] | None = None,
) -> SpawnedAgent:
    """Gateway-side spawn (Task #1236 follow-up): preflight -> create the agent
    ROW in-process -> forward a launch-only op to the target runner.

    The target runner's ops server runs as the least-privilege `ava_runner`
    role, which by design cannot INSERT agents / agents_meta — so the row is
    created HERE, in the gateway process, as the main data-plane identity. The
    forward op (`kind="spawn-launch"`) validates and wakes the hosted runner.
    The first prompt is committed with the row before this forward.

    Every spawn in the system funnels through this helper
    (POST /api/keyed/v1/agents, the guide / packages / schedules draft routers,
    the MCP tools server), so preflight, row creation, and launch stay uniform
    across entry points. Each creation is keyed: `creation_key` is the
    principal-bound identity of one intent, and `creation_identity` (default:
    the parsed request) is what a replay must match.
    """

    def default_model_reader() -> str:
        return cast(str, authority.service_field_value("llm_model"))

    request_hash = creation_request_hash(
        creation_identity if creation_identity is not None else body.model_dump(mode="json")
    )
    existing = await asyncio.to_thread(creation_receipt, pool, creation_key, request_hash)
    if existing is not None:
        return await recover_launch(
            pool,
            db,
            bus,
            existing,
            catalog=catalog,
            default_model_reader=default_model_reader,
        )
    preset_name, tail_skills, model_receipt = await asyncio.to_thread(
        spawn_preflight_blocking,
        db,
        target,
        body,
        pool,
        catalog=catalog,
        authority=authority,
    )
    # fork_checkpoint resolution stays gateway-side: LangGraph checkpoints are
    # append-only and "latest" drifts under concurrent writes, so the gateway
    # resolves an explicit id before creating the row.
    fork_checkpoint = await asyncio.to_thread(
        spawn_prechecks_blocking, body, pool, catalog=catalog, authority=authority
    )
    try:
        new_id, birth_config, prompt_inbound_id, launch_attempt_id = await asyncio.to_thread(
            create_agent_row,
            db,
            bus,
            catalog=catalog,
            authority=authority,
            spawner=body.spawner,
            fork_from=body.fork_from,
            fork_checkpoint=fork_checkpoint,
            machine=target,
            config=body.config,
            label=body.label,
            preset_name=preset_name,
            fork_tail_skills=tail_skills,
            prompt=body.prompt,
            prompt_source=body.prompt_source,
            creation_key=creation_key,
            creation_request_hash=request_hash,
        )
    except CreationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # A concurrent caller may have won after this caller's preflight. Its
    # committed placement/config/attempt are authoritative for recovery.
    committed = await asyncio.to_thread(creation_receipt, pool, creation_key, request_hash)
    if committed is None:
        raise RuntimeError("committed agent creation receipt is missing")
    if not committed.launch_pending:
        return await recover_launch(
            pool,
            db,
            bus,
            committed,
            catalog=catalog,
            default_model_reader=default_model_reader,
        )
    target, birth_config, launch_attempt_id = (
        committed.machine,
        committed.birth_config,
        committed.launch_attempt_id,
    )
    body.config = committed.config
    await announce_creation_prompt(bus, new_id, prompt_inbound_id, body)
    launch = LaunchAgentRequest(
        agent_id=new_id,
        launch_attempt_id=launch_attempt_id,
        config=body.config,
        birth_config=birth_config,
    )
    # The endpoint response is the launch op's verdict (the launched agent id —
    # equal to new_id in production; the runner answers for the launch). A
    # withdrawal settlement travels as the spawner's receipt (task #4306).
    spawned = await _dispatch_committed_launch(
        pool, db, bus, target, launch, catalog=catalog, default_model_reader=default_model_reader
    )
    if model_receipt is not None:
        spawned = spawned.model_copy(
            update={
                "config_normalized": ConfigNormalization(
                    requested=model_receipt[0], resolved=model_receipt[1]
                )
            }
        )
    return await _accepted_launch_receipt(
        pool, spawned, catalog=catalog, default_model_reader=default_model_reader
    )


async def recover_launch(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    existing: CreationReceipt,
    *,
    catalog: ModelCatalog,
    default_model_reader: Callable[[], str],
) -> SpawnedAgent:
    """Recover only an unadmitted birth; a completed incarnation is never revived."""
    if not existing.launch_pending:
        return SpawnedAgent(id=existing.agent_id, accepted=True, execution_observed=False)
    spawned = await _dispatch_committed_launch(
        pool,
        db,
        bus,
        existing.machine,
        LaunchAgentRequest(
            agent_id=existing.agent_id,
            launch_attempt_id=existing.launch_attempt_id,
            config=existing.config,
            birth_config=existing.birth_config,
        ),
        catalog=catalog,
        default_model_reader=default_model_reader,
    )
    return await _accepted_launch_receipt(
        pool, spawned, catalog=catalog, default_model_reader=default_model_reader
    )


async def announce_creation_prompt(
    bus: EventBus, agent_id: int, prompt_inbound_id: int | None, body: SpawnAgentRequest
) -> None:
    """A missed live hint cannot invalidate a committed birth and prompt."""
    from ops.lifecycle.events import publish_inbound_arrived

    if prompt_inbound_id is None or body.prompt_source is None or body.prompt is None:
        return
    try:
        await publish_inbound_arrived(
            bus,
            agent_id,
            prompt_inbound_id,
            "chat",
            body.prompt_source,
            spawn_prompt_with_label(body.prompt, body.label),
        )
    except Exception as exc:
        logger.warning("created agent {} inbound hint failed: {}", agent_id, type(exc).__name__)


def guarded_draft_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """Require a verified principal and the exact versioned operation namespace."""
    if idempotency_scope != PRINCIPAL_SCOPE:
        raise HTTPException(status_code=422, detail="guarded draft requires principal-v1 scope")
    return scoped_creation_key(request, idempotency_key, operation_path=request.url.path)


def spawn_preflight_blocking(
    db: Database,
    target: str,
    body: SpawnAgentRequest,
    pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
) -> tuple[str | None, list[str] | None, tuple[str, str] | None]:
    """Sync spawn preflight — via to_thread: registry capability check, preset
    fold, fork config rule + tail-skills delta, model-config settlement and
    validation (may read provider API keys). Returns ``(preset_name,
    tail_skills, model_receipt)`` for the agent row / fork inbound; the receipt
    is ``(requested, resolved)`` when a withdrawn ``llm_model`` was rewritten,
    else None.
    """
    from base.api_contracts.agent_contract import MachinePaused
    from base.cluster.machines import is_paused, lookup_role

    # Capability is read from the cluster registry — the same source the forward
    # resolves the target's ops URL from — uniformly for every target, local
    # included. The gateway is a pure router: it never introspects its own process
    # role, so a co-located runner is just a registry entry whose ops URL happens
    # to be localhost. An unregistered target raises MachineNotRegistered (404 +
    # reason via the app's AvaAgentError handler) straight out of lookup_role.
    if "agent-runner" not in lookup_role(db, target):
        # A proper wire error (carries `reason`) — not a bare HTTPException, whose
        # reason-less body trips the SDK's `raise_from_response` into a confusing
        # `KeyError: 'reason'` that masks this message.
        raise SpawnTargetNotAgentRunner(
            f"spawn target {target!r} has no agent-runner capability; agents run on "
            "agent-runner machines. Pass body.machine = <an agent-runner name> "
            "(see ava.agents.list_machines())."
        )
    # A paused machine is deliberately out of the cluster: its ops server may be
    # unreachable and its agents were terminated on pause. Refuse with a precise
    # 409 (MachinePaused) instead of forwarding into an unreachable dial — this
    # is the one enforcement point every spawn goes through, so schedules /
    # watcher respawns / peer spawns targeting a paused machine all fail with
    # the same clear reason instead of a transport error.
    if is_paused(db, target):
        raise MachinePaused(
            f"spawn target {target!r} is paused (temporarily removed from the "
            "cluster); resume it first with `ava cluster resume <name>` on the "
            "gateway, then spawn."
        )
    preset_name, tail_skills = _normalize_and_resolve_preset(pool, body)
    if body.fork_from is not None:
        preset_name, tail_skills = _validate_fork_config(pool, body, preset_name)
    # Settle a withdrawn llm_model BEFORE the overlay is stored or forwarded —
    # the row, fork copies and the launch op all carry the registered fallback,
    # and the spawner gets a receipt (response field + log line) instead of the
    # withdrawal surfacing later as a wake-time normalization (task #4306: the
    # 9/20-21 recurrence wrote 12 retired ids through this path, silently).
    model_receipt: tuple[str, str] | None = None
    if body.config:
        from base.lm.registry import normalize_overlay_llm_model

        model_receipt = normalize_overlay_llm_model(body.config, models=catalog.models)
    # Validate model config before forwarding — fail fast at the gateway
    # instead of letting the agent process silently hang on a missing API key.
    from base.agents.model.model_config import validate_spawn_model_config

    with pool.connection() as conn, conn.cursor() as cur:
        validate_spawn_model_config(
            cur,
            body.config,
            body.fork_from,
            catalog=catalog,
            authority=authority,
            llm_override=settings.lm.llm_override,
        )
    if model_receipt is not None:
        logger.warning(
            "spawn config_overlay llm_model {requested!r} is withdrawn; stored "
            "the registered fallback {resolved!r} (task #4306)",
            event="spawn_config_normalized",
            requested=model_receipt[0],
            resolved=model_receipt[1],
        )
    return preset_name, tail_skills, model_receipt


# The overlay key naming a preset, and the only overlay keys a fork may change
# (additions only — see _validate_fork_config).
_PRESET_KEY = "preset"
_FORK_SKILL_KEYS = ("skills_to_inject_into_system_prompt", "skills_to_expand_at_start")


def _normalize_and_resolve_preset(
    pool: ConnectionPool, body: SpawnAgentRequest
) -> tuple[str | None, list[str] | None]:
    """Resolve `config_overlay.preset` into the effective overlay, returning the
    preset name.

    The preset's stored config is the base; the explicit `body.config` fields
    win per-key (explicit beats template). The resolved map is written back to
    `body.config` WITHOUT the preset key, so the forwarded spawn carries only a
    plain config — the runner never sees the preset. The name is returned
    separately: it is stored on the agent row (`agents_meta.preset_name`) purely
    for display, next to the resolved overlay.

    400 when the overlay key is not a non-empty string, or when the named preset
    does not exist (a spawn referencing a missing preset is a caller error,
    surfaced up front rather than silently ignored).
    """
    explicit = body.config or {}
    preset_name = explicit.get(_PRESET_KEY)
    if preset_name is None:
        return None, None
    if not isinstance(preset_name, str) or not preset_name.strip():
        raise HTTPException(
            status_code=400,
            detail=f"config_overlay.preset must be a non-empty string, got {preset_name!r}",
        )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT config FROM agent_presets WHERE name = %s", (preset_name,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail=f"preset {preset_name!r} not found")
    preset_config: dict[str, object] = row[0]
    explicit_fields = {k: v for k, v in explicit.items() if k != _PRESET_KEY}
    body.config = {**preset_config, **explicit_fields}
    return preset_name, None


def _as_str_list(value: object) -> list[str] | None:
    """A skill list as typed as it can be: None for anything that is not a
    list of strings (missing key included) — the fork rule only reasons about
    real skill lists."""
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else None


def _fork_source(
    pool: ConnectionPool, source_id: int | None
) -> tuple[dict[str, object], str | None, dict[str, object]]:
    """`(config_overlay, preset_name, birth_config)` of the fork source agent."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, preset_name, birth_config FROM agents_meta WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"fork source agent {source_id} does not exist")
    return row[0] or {}, row[1], row[2] or {}


def _offending_fork_keys(
    merged: dict[str, object],
    source_overlay: dict[str, object],
    source_effective: dict[str, object],
    fork_effective: dict[str, object],
) -> list[str]:
    """Overlay keys the fork may not change: any non-skill-list difference or a skill reduction."""
    offending: list[str] = []
    for key in set(merged) | set(source_overlay):
        if fork_effective.get(key) == source_effective.get(key):
            continue
        if key not in _FORK_SKILL_KEYS:
            offending.append(key)
            continue
        fork_list = _as_str_list(fork_effective.get(key))
        source_list = _as_str_list(source_effective.get(key))
        if fork_list is None or source_list is None or not set(source_list) <= set(fork_list):
            offending.append(key)
    return offending


def _validate_fork_config(
    pool: ConnectionPool, body: SpawnAgentRequest, preset_name: str | None
) -> tuple[str | None, list[str] | None]:
    """Enforce the fork config rule and compute the tail-graft skill delta.

    A fork must keep the source agent's effective config
    (`{**birth_config, **config_overlay}`) so the inherited context stays
    truthful and its cached prefix survives. The only sanctioned change:
    ADDING skills to `skills_to_inject_into_system_prompt` /
    `skills_to_expand_at_start` (superset of the source's lists) — those load
    at the context tail, never in the cached prefix.

    - fork without config: `body.config` becomes a copy of the source's
      resolved `config_overlay` (the fork runs exactly what the source ran);
      the effective preset name is the source's when the fork named none.
    - fork with config: the stored overlay becomes source overlay + delta;
      any non-skill-list difference or a skill-list reduction raises
      ForkConfigChangeNotAllowed (400).

    Returns `(preset_name, tail_skills)` where `tail_skills` lists the
    inject-list skills the fork added that its preloaded (expand) set does not
    already graft — carried in the fork inbound's payload for the claim node
    to append at the tail. Raises AgentNotFound when the source row is gone.
    """
    source_overlay, source_preset, source_birth = _fork_source(pool, body.fork_from)

    fork_overlay = body.config
    if fork_overlay is None:
        # No config change requested: inherit the source's overlay verbatim so
        # the fork's effective config equals the source's (the pre-2026-09-10
        # behavior dropped the source's overlay, silently re-braining the fork).
        body.config = dict(source_overlay) if source_overlay else None
        return (preset_name or source_preset), None

    # The fork's stored overlay is the SOURCE's overlay plus the fork's own
    # fields; the diff runs on that merged map (fields the fork names that
    # simply repeat the source's value are a no-op, not a change).
    merged = {**source_overlay, **fork_overlay}
    source_effective = {**source_birth, **source_overlay}
    fork_effective = {**source_birth, **merged}
    offending = _offending_fork_keys(merged, source_overlay, source_effective, fork_effective)
    if offending:
        raise ForkConfigChangeNotAllowed(
            f"fork may not change config overlay keys {sorted(offending)!r} — a fork "
            f"keeps the source agent's config so its context cache stays valid; only "
            f"ADDING skills to {_FORK_SKILL_KEYS!r} is allowed (loaded at the context "
            "tail). Change the fork after it exists via restart(config_overlay=...) "
            "instead."
        )
    body.config = merged
    inject_key, expand_key = _FORK_SKILL_KEYS
    source_inject = set(_as_str_list(source_effective.get(inject_key)) or [])
    fork_inject = set(_as_str_list(fork_effective.get(inject_key)) or [])
    fork_expand = set(_as_str_list(fork_effective.get(expand_key)) or [])
    delta = sorted(s for s in fork_inject - source_inject if s not in fork_expand)
    return (preset_name or source_preset), (delta or None)


async def _accepted_launch_receipt(
    pool: ConnectionPool,
    spawned: SpawnedAgent,
    *,
    catalog: ModelCatalog,
    default_model_reader: Callable[[], str],
) -> SpawnedAgent:
    observed = await asyncio.to_thread(
        _creation_availability,
        pool,
        spawned.id,
        catalog=catalog,
        default_model_reader=default_model_reader,
    )
    return spawned.model_copy(
        update={
            "accepted": True,
            "execution_observed": False,
            "reason": observed.reason,
            "observed_at": observed.observed_at,
        }
    )


def _mark_launch_failure(
    pool: ConnectionPool, bus: EventBus, agent_id: int, attempt_id: UUID, reason: AvailabilityReason
) -> None:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_launch_failure_reason=%s, "
            "last_launch_failure_at=clock_timestamp() "
            "WHERE id=%s AND last_launch_attempt_id=%s AND status='idling' "
            "AND last_admission_at IS NULL",
            (reason.value, agent_id, attempt_id),
        )
        changed = cur.rowcount > 0
    if changed:
        publish_agent_updated_sync(bus, agent_id)


def _clear_launch_failure(
    pool: ConnectionPool, bus: EventBus, agent_id: int, attempt_id: UUID
) -> None:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_launch_failure_reason=NULL, last_launch_failure_at=NULL "
            "WHERE id=%s AND last_launch_attempt_id=%s",
            (agent_id, attempt_id),
        )
        changed = cur.rowcount > 0
    if changed:
        publish_agent_updated_sync(bus, agent_id)


def _read_launch_state(
    pool: ConnectionPool,
    agent_id: int,
    *,
    catalog: ModelCatalog,
    default_model_reader: Callable[[], str],
) -> tuple[dict[str, object], bool, bool]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, last_admission_at, last_launch_attempt_id FROM agents_meta WHERE id=%s",
            (agent_id,),
        )
        row = cur.fetchone()
        snapshot = snapshot_module.select_one(
            conn, agent_id, catalog=catalog, default_model_reader=default_model_reader
        )
    if row is None or snapshot is None:
        return {"status": "unknown", "availability": None}, False, False
    status, admission_at, attempt_id = row
    return (
        {
            "status": status,
            "availability": snapshot.availability.model_dump(mode="json")
            if snapshot.availability is not None
            else None,
        },
        admission_at is not None and status != "terminated",
        status == "idling" and admission_at is None and attempt_id is not None,
    )


async def _dispatch_committed_launch(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    target: str,
    launch: LaunchAgentRequest,
    *,
    catalog: ModelCatalog,
    default_model_reader: Callable[[], str],
) -> SpawnedAgent:
    attempt_id = launch.launch_attempt_id
    try:
        spawned = await forward_spawn_to_remote(db, target, launch)
        _require_matching_launch_receipt(spawned, launch.agent_id, target)
    except Exception as exc:
        reason = (
            exc.reason
            if isinstance(exc, forward.LaunchForwardError)
            else AvailabilityReason.LAUNCH_UNKNOWN
        )
        detail = str(exc) if isinstance(exc, forward.LaunchForwardError) else type(exc).__name__
        logger.warning(
            "agent {} launch dispatch failed on {} attempt {}: {} ({})",
            launch.agent_id,
            target,
            attempt_id,
            reason.value,
            detail,
        )
        state: dict[str, object]
        try:
            await asyncio.to_thread(
                _mark_launch_failure, pool, bus, launch.agent_id, attempt_id, reason
            )
            state, admitted, retry_legal = await asyncio.to_thread(
                _read_launch_state,
                pool,
                launch.agent_id,
                catalog=catalog,
                default_model_reader=default_model_reader,
            )
        except Exception as persistence_exc:
            # The retry endpoint re-reads status/attempt once Postgres returns.
            state = {"status": "unknown", "availability": None}
            admitted = False
            retry_legal = True
            detail += f"; launch-state persistence/read failed ({type(persistence_exc).__name__})"
        if admitted:
            return SpawnedAgent(id=launch.agent_id)
        raise AgentLaunchFailed(
            f"Agent #{launch.agent_id} was created; launch dispatch to {target} failed: {detail}",
            agent_id=launch.agent_id,
            state=state,
            retry_launch_path=(
                f"/api/keyed/v1/agents/{launch.agent_id}/retry-launch" if retry_legal else None
            ),
        ) from exc
    try:
        await asyncio.to_thread(_clear_launch_failure, pool, bus, launch.agent_id, attempt_id)
    except Exception as exc:
        logger.warning(
            "agent {} accepted launch but failure clear failed: {}",
            launch.agent_id,
            type(exc).__name__,
        )
    return spawned


def _require_matching_launch_receipt(spawned: SpawnedAgent, agent_id: int, target: str) -> None:
    if spawned.id != agent_id:
        raise forward.LaunchForwardError(
            AvailabilityReason.LAUNCH_UNREACHABLE,
            f"target machine={target!r} returned agent {spawned.id} for {agent_id}",
        )


def _creation_availability(
    pool: ConnectionPool,
    agent_id: int,
    *,
    catalog: ModelCatalog,
    default_model_reader: Callable[[], str],
) -> AgentAvailability:
    try:
        with pool.connection() as conn:
            snap = snapshot_module.select_one(
                conn, agent_id, catalog=catalog, default_model_reader=default_model_reader
            )
    except Exception as exc:
        logger.warning(
            "created agent {} receipt read failed ({}): {}",
            agent_id,
            type(exc).__name__,
            exc,
        )
        return AgentAvailability(reason=AvailabilityReason.UNKNOWN, observed_at=datetime.now(UTC))
    if snap is None or snap.availability is None:
        logger.warning("created agent {} unavailable for receipt read", agent_id)
        return AgentAvailability(reason=AvailabilityReason.UNKNOWN, observed_at=datetime.now(UTC))
    return snap.availability
