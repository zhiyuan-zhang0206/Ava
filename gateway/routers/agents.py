"""Agent CRUD + spawn endpoints — /api/agents/*.

Covers list / get / spawn / label patch, the birth-chain read
(`GET /api/agents/{id}/born-chain` — the light half of `/neighbors` that the
inherited-memory context note resolves), plus the model registry
(`GET /api/models`) the spawn dialog renders. The lifecycle surface
(compact / cancel / terminate / exited / resurrect / restart)
lives in routers/agents_lifecycle.py; message + state reads (messages /
trace / last-message / pending / activity / token-usage / context-breakdown)
in routers/agents_state.py; the cross-machine forward helpers shared by all
of them in routers/agents_forward.py. The per-agent events endpoints (SSE
live tail + historical REST query) live in routers/agent_events.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from gateway import neighbors
from gateway.routers import agents_forward
from gateway.routers.agents_forward import _forward_spawn_to_remote
from gateway.schemas import (
    AgentRow,
    BornChainResponse,
    BornChainRow,
    LabelPatchRequest,
    ModelsResponse,
)
from ops.agent_spawn import create_agent_row
from ops.ops_lifecycle import _spawn_prechecks_blocking
from ops.rpc_schemas import (
    ConfigNormalization,
    LaunchAgentRequest,
    SpawnAgentRequest,
    SpawnedAgent,
)
from shared import agent_roster, agent_snapshot
from shared.agent_observation import AgentAvailability, AvailabilityReason
from shared.agents import (
    AgentLaunchFailed,
    AgentNotFound,
    ForkConfigChangeNotAllowed,
    InvalidModelConfig,
    SpawnTargetNotAgentRunner,
)
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.labels import publish_label_updated, spawn_prompt_with_label
from shared.live_announce import publish_agent_updated_sync
from shared.log import logger
from shared.machine import machine_name

router = APIRouter()


@router.patch("/api/agents/{agent_id}", status_code=204)
async def patch_agent(agent_id: int, body: LabelPatchRequest, request: Request) -> Response:
    """Manually set / reset an agent label.

    body.label non-empty: UPDATE directly. Empty string (after strip):
    UPDATE label=NULL to reset back to "not set"; frontend re-displays
    fallback `#N`. Both cases also set `label_user_set=TRUE` — sticky bit
    so the background LLM CAS no longer overwrites (otherwise after the
    user resets, the LLM would still match `label IS NULL` and rename it,
    defeating the reset intent). Both cases publish LabelUpdated so SSE
    pushes in real time.

    404: agent_id does not exist.
    """
    new_label: str | None = body.label if body.label else None
    await asyncio.to_thread(_patch_label_blocking, request.app.state.db_pool, agent_id, new_label)
    await publish_label_updated(agent_id, new_label)
    return Response(status_code=204)


@router.get("/api/models")
def get_models() -> ModelsResponse:
    """List selectable LLM models (grouped by provider) + the cluster default.

    Roster and tuning come from the model registry; current rates come from
    the versioned pricing catalog. The default mirrors `settings.lm.llm_model`
    so the UI can pre-select it.
    """
    from gateway.schemas import ModelInfo, ModelPricing
    from shared.lm.factory import SUPPORTED_MODELS, ensure_provider_plugins_loaded
    from shared.lm.pricing import rates_at

    # Plugin provider models register here (once per process) — the spawn
    # dropdown must list them even though the gateway never loads plugin.py.
    ensure_provider_plugins_loaded()
    from shared.lm.registry import MODELS, explain_setting

    # Stable model facts come off the registry; volatile prices come off the
    # effective-dated catalog. `effort_levels` is the same vocabulary the factory
    # clamps onto at model build (for extended-thinking-only models like
    # claude-haiku-4-5 it is the binary thinking on/off vocabulary), so the
    # dropdown and the wire behavior cannot drift apart.
    models: dict[str, ModelInfo] = {}
    for provider, model_list in SUPPORTED_MODELS.items():
        for model in model_list:
            spec = MODELS[model]
            rates = rates_at(model, input_tokens=0)
            if rates is None:
                raise RuntimeError(f"spawnable model {model!r} has no current catalog price")
            pricing = ModelPricing(
                input=rates.cache_miss,
                cache_read=rates.cache_hit,
                output=rates.output,
            )
            # The model's default effort: the per-model tuning layer, resolved
            # through the registry's layering (NOT the raw field — same code
            # path agents use, minus the explicit-env layer: the picker shows
            # the model's own default, while a cluster-wide AVA_REASONING_EFFORT
            # pin is operator policy visible in the config panel's per-model
            # view). Validation guarantees spawnable models pin a concrete
            # value; "" (provider's own default) surfaces as None for any
            # model that slipped through without one.
            resolved_effort = explain_setting("reasoning_effort", model=model, explicit=None).value
            models[model] = ModelInfo(
                provider=provider,
                context_window=spec.context_window or 0,
                pricing=pricing,
                reasoning_effort_options=(
                    list(spec.effort_levels) if spec.effort_levels is not None else None
                ),
                reasoning_effort_default=resolved_effort or None,
                superseded_by=spec.superseded_by,
            )

    return ModelsResponse(
        providers=SUPPORTED_MODELS,
        models=models,
        default=settings.lm.llm_model,
    )


@router.get("/api/agents")
def get_agents(
    request: Request,
    scope: Annotated[agent_roster.AgentDirectoryScope, Query()] = "live",
    query: Annotated[str, Query(max_length=200)] = "",
    before_id: Annotated[int | None, Query(gt=0, le=9223372036854775807)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> agent_roster.AgentDirectoryPage:
    """Read one directory page. History is explicit and never fetched implicitly."""
    with request.app.state.db_pool.connection() as conn:
        return agent_roster.list_directory(
            conn,
            scope=scope,
            query=query,
            before_id=before_id,
            limit=limit,
        )


@router.get("/api/agents/roster")
def get_agent_roster(request: Request) -> agent_roster.AgentRoster:
    """Read the live tree and its necessary ancestor links in one snapshot."""
    with request.app.state.db_pool.connection() as conn:
        return agent_roster.select_roster(conn)


def _patch_label_blocking(pool: ConnectionPool, agent_id: int, new_label: str | None) -> None:
    """Sync label UPDATE + 404 guard — via to_thread."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents SET label=%s, label_user_set=TRUE WHERE id=%s",
            (new_label, agent_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")


def _spawn_preflight_blocking(
    target: str, body: SpawnAgentRequest, pool: ConnectionPool
) -> tuple[str | None, list[str] | None, tuple[str, str] | None]:
    """Sync spawn preflight — via to_thread: registry capability check, preset
    fold, fork config rule + tail-skills delta, model-config settlement and
    validation (may read provider API keys). Returns ``(preset_name,
    tail_skills, model_receipt)`` for the agent row / fork inbound; the receipt
    is ``(requested, resolved)`` when a withdrawn ``llm_model`` was rewritten,
    else None.
    """
    from shared.agents import MachinePaused
    from shared.machines import is_paused, lookup_role

    # Capability is read from the cluster registry — the same source the forward
    # resolves the target's ops URL from — uniformly for every target, local
    # included. The gateway is a pure router: it never introspects its own process
    # role, so a co-located runner is just a registry entry whose ops URL happens
    # to be localhost. An unregistered target raises MachineNotRegistered (404 +
    # reason via the app's AvaAgentError handler) straight out of lookup_role.
    if "agent-runner" not in lookup_role(target):
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
    if is_paused(target):
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
        from shared.lm.registry import normalize_overlay_llm_model

        model_receipt = normalize_overlay_llm_model(body.config)
    # Validate model config before forwarding — fail fast at the gateway
    # instead of letting the agent process silently hang on a missing API key.
    from shared.lm.factory import validate_model_config

    try:
        validate_model_config(model=settings.lm.llm_model, config=body.config)
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc
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

    The former top-level `body.preset` field is retired (task #4086): a non-null
    value is refused with a 400 pointing at the overlay key, while a null — the
    field default, which a client rolling through the compatibility window may
    still send explicitly — is tolerated as unset so such a client keeps
    spawning.

    400 when the overlay key is not a non-empty string, or when the named preset
    does not exist (a spawn referencing a missing preset is a caller error,
    surfaced up front rather than silently ignored).
    """
    if body.preset is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "the top-level preset field is retired — pass the preset as "
                'config_overlay={"preset": "<name>"}'
            ),
        )
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
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, preset_name, birth_config FROM agents_meta WHERE id = %s",
            (body.fork_from,),
        )
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"fork source agent {body.fork_from} does not exist")
    source_overlay: dict[str, object] = row[0] or {}
    source_preset: str | None = row[1]
    source_birth: dict[str, object] = row[2] or {}

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
    offending: list[str] = []
    for key in set(merged) | set(source_overlay):
        if fork_effective.get(key) == source_effective.get(key):
            continue
        if key not in _FORK_SKILL_KEYS:
            offending.append(key)
            continue
        fork_list = _as_str_list(fork_effective.get(key))
        source_list = _as_str_list(source_effective.get(key))
        if fork_list is None or source_list is None:
            offending.append(key)
            continue
        if not set(source_list) <= set(fork_list):
            offending.append(key)
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


async def create_and_launch_agent(
    body: SpawnAgentRequest, target: str, pool: ConnectionPool
) -> SpawnedAgent:
    """Gateway-side spawn (Task #1236 follow-up): preflight -> create the agent
    ROW in-process -> forward a launch-only op to the target runner.

    The target runner's ops server runs as the least-privilege `ava_runner`
    role, which by design cannot INSERT agents / agents_meta — so the row is
    created HERE, in the gateway process, as the main data-plane identity. The
    forward op (`kind="spawn-launch-v2"`) validates and wakes the hosted runner.
    The first prompt is committed with the row before this forward.

    Every spawn in the system funnels through this helper (POST /api/agents,
    the guide / packages / schedules draft routers, the MCP tools server), so
    preflight, row creation, and launch stay uniform across entry points.
    """
    preset_name, tail_skills, model_receipt = await asyncio.to_thread(
        _spawn_preflight_blocking, target, body, pool
    )
    # fork_checkpoint resolution stays gateway-side: LangGraph checkpoints are
    # append-only and "latest" drifts under concurrent writes, so the gateway
    # resolves an explicit id before creating the row.
    fork_checkpoint = await asyncio.to_thread(_spawn_prechecks_blocking, body, pool)
    new_id, birth_config, prompt_inbound_id, launch_attempt_id = await asyncio.to_thread(
        create_agent_row,
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
    )
    if prompt_inbound_id is not None and body.prompt_source is not None and body.prompt is not None:
        from ops.ops_events import publish_inbound_arrived

        prompt_content = spawn_prompt_with_label(body.prompt, body.label)
        try:
            await publish_inbound_arrived(
                new_id, prompt_inbound_id, "chat", body.prompt_source, prompt_content
            )
        except Exception as exc:
            logger.warning("created agent {} inbound hint failed: {}", new_id, type(exc).__name__)
    launch = LaunchAgentRequest(
        agent_id=new_id,
        launch_attempt_id=launch_attempt_id,
        config=body.config,
        birth_config=birth_config,
    )
    # The endpoint response is the launch op's verdict (the launched agent id —
    # equal to new_id in production; the runner answers for the launch). A
    # withdrawal settlement travels as the spawner's receipt (task #4306).
    spawned = await _dispatch_committed_launch(pool, target, launch)
    if model_receipt is not None:
        spawned = spawned.model_copy(
            update={
                "config_normalized": ConfigNormalization(
                    requested=model_receipt[0], resolved=model_receipt[1]
                )
            }
        )
    return await _accepted_launch_receipt(pool, spawned)


async def _accepted_launch_receipt(pool: ConnectionPool, spawned: SpawnedAgent) -> SpawnedAgent:
    observed = await asyncio.to_thread(_creation_availability, pool, spawned.id)
    return spawned.model_copy(
        update={
            "accepted": True,
            "execution_observed": False,
            "reason": observed.reason,
            "observed_at": observed.observed_at,
        }
    )


def _mark_launch_failure(
    pool: ConnectionPool, agent_id: int, attempt_id: UUID, reason: AvailabilityReason
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
        publish_agent_updated_sync(agent_id)


def _clear_launch_failure(pool: ConnectionPool, agent_id: int, attempt_id: UUID) -> None:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_launch_failure_reason=NULL, last_launch_failure_at=NULL "
            "WHERE id=%s AND last_launch_attempt_id=%s",
            (agent_id, attempt_id),
        )
        changed = cur.rowcount > 0
    if changed:
        publish_agent_updated_sync(agent_id)


def _read_launch_state(pool: ConnectionPool, agent_id: int) -> tuple[dict[str, object], bool, bool]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, last_admission_at, last_launch_attempt_id FROM agents_meta WHERE id=%s",
            (agent_id,),
        )
        row = cur.fetchone()
        snapshot = agent_snapshot.select_one(conn, agent_id)
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
    pool: ConnectionPool, target: str, launch: LaunchAgentRequest
) -> SpawnedAgent:
    attempt_id = launch.launch_attempt_id
    if attempt_id is None:
        raise RuntimeError("committed launch is missing its attempt ID")
    try:
        spawned = await _forward_spawn_to_remote(target, launch)
        _require_matching_launch_receipt(spawned, launch.agent_id, target)
    except Exception as exc:
        reason = (
            exc.reason
            if isinstance(exc, agents_forward.LaunchForwardError)
            else AvailabilityReason.LAUNCH_UNKNOWN
        )
        detail = (
            str(exc) if isinstance(exc, agents_forward.LaunchForwardError) else type(exc).__name__
        )
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
            await asyncio.to_thread(_mark_launch_failure, pool, launch.agent_id, attempt_id, reason)
            state, admitted, retry_legal = await asyncio.to_thread(
                _read_launch_state, pool, launch.agent_id
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
                f"/api/agents/{launch.agent_id}/retry-launch" if retry_legal else None
            ),
        ) from exc
    try:
        await asyncio.to_thread(_clear_launch_failure, pool, launch.agent_id, attempt_id)
    except Exception as exc:
        logger.warning(
            "agent {} accepted launch but failure clear failed: {}",
            launch.agent_id,
            type(exc).__name__,
        )
    return spawned


def _require_matching_launch_receipt(spawned: SpawnedAgent, agent_id: int, target: str) -> None:
    if spawned.id != agent_id:
        raise agents_forward.LaunchForwardError(
            AvailabilityReason.LAUNCH_UNREACHABLE,
            f"target machine={target!r} returned agent {spawned.id} for {agent_id}",
        )


def _creation_availability(pool: ConnectionPool, agent_id: int) -> AgentAvailability:
    try:
        with pool.connection() as conn:
            snap = agent_snapshot.select_one(conn, agent_id)
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


@router.post("/api/agents", status_code=201, response_model_exclude_none=True)
async def post_agents(body: SpawnAgentRequest, request: Request) -> SpawnedAgent:
    """Spawn a new agent — uniform HTTP path for SDK / frontend / scripts.

    Spawn is HTTP-uniform: every spawn funnels through
    `create_and_launch_agent`, which creates the agent row in the gateway
    process (the main data-plane identity — the target runner's ops server
    runs as the least-privilege `ava_runner` role and cannot INSERT agents)
    and forwards a launch-only op to the target runner's ops server, whether
    the runner is remote or co-located on this box (localhost) — one code
    path, uniform logs/traces, no in-process shortcut. Auto label generation
    is done asynchronously by the services/labeler daemon — does not block
    the spawn response.

    Plain and fork first prompts commit with the row. The fork marker precedes
    its chat in that transaction. InboundArrived is a best-effort live hint;
    the pending scan recovers a missed wake.

    body.machine = None targets the local machine (which must be a registered
    agent-runner).

    400: the target machine is registered but has no agent-runner capability
    (wire `reason='spawn_target_not_agent_runner'`). 404: the target is not in the
    registry. 409: the fork_from agent has no checkpoint (no LLM/exec step yet).
    """
    target = body.machine if body.machine is not None else machine_name()
    return await create_and_launch_agent(body, target, request.app.state.db_pool)


def _prepare_retry_launch(
    pool: ConnectionPool, agent_id: int
) -> tuple[str, LaunchAgentRequest | None]:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, config_overlay, birth_config, status, last_admission_at, "
            "last_launch_attempt_id FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        target, config, birth_config, status, admitted_at, prior_attempt = row
        if admitted_at is not None and status != "terminated":
            return target, None
        if status != "idling" or prior_attempt is None:
            raise HTTPException(
                status_code=409,
                detail=f"agent {agent_id} cannot retry launch in status {status}",
            )
        attempt_id = uuid4()
        cur.execute(
            "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s",
            (attempt_id, agent_id),
        )
    return target, LaunchAgentRequest(
        agent_id=agent_id,
        launch_attempt_id=attempt_id,
        config=config,
        birth_config=birth_config,
    )


@router.post("/api/agents/{agent_id}/retry-launch", response_model_exclude_none=True)
async def retry_agent_launch(agent_id: int, request: Request) -> SpawnedAgent:
    """Retry dispatch for one committed identity without adding an inbound."""
    pool = request.app.state.db_pool
    target, launch = await asyncio.to_thread(_prepare_retry_launch, pool, agent_id)
    if launch is None:
        return await _accepted_launch_receipt(pool, SpawnedAgent(id=agent_id))
    spawned = await _dispatch_committed_launch(pool, target, launch)
    return await _accepted_launch_receipt(pool, spawned)


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: int, request: Request) -> AgentRow:
    """Full detail addressed by ID, including terminated agents outside loaded pages.

    The browser and SDK status readers use this independent selection boundary.
    Notice bodies stay here and in the Inbox; directory cards contain counts.
    A nonexistent ID returns 404 rather than falling back to another agent.
    """
    with request.app.state.db_pool.connection() as conn:
        snap = agent_snapshot.select_one(conn, agent_id)
    if snap is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return AgentRow.model_validate(snap.model_dump())


@router.get("/api/agents/{agent_id}/born-chain")
def get_agent_born_chain(agent_id: int, request: Request) -> BornChainResponse:
    """The immutable birth chain above `agent_id`, nearest ancestor first
    (1 = direct birth parent) — one recursive `agents_meta.born_spawner` walk.

    The light half of `/neighbors`, and separate on purpose: the inherited
    memory context note resolves this chain at every window establishment, and
    it must not drag the tie graph's frozen-archive read + Loki live tail into
    that path. Adds `machine` (which ancestors a reader can open locally) and
    keeps `status` (the chain includes terminated ancestors). Terminates only
    when `born_spawner` is not an `agent:N` value (a user / external spawn).

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    """
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
    chain = neighbors.born_chain(request.app.state.db_pool, root=agent_id)
    detail: dict[int, tuple[str | None, str, str | None]] = {}
    ids = [r[0] for r in chain]
    if ids:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.id, t.label, m.status, m.machine
                FROM agents t
                JOIN agents_meta m ON m.id = t.id
                WHERE t.id = ANY(%s)
                """,
                (ids,),
            )
            detail = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    return BornChainResponse(
        ancestors=[
            BornChainRow(
                agent_id=agent,
                label=detail.get(agent, (None, "terminated", None))[0],
                status=detail.get(agent, (None, "terminated", None))[1],
                machine=detail.get(agent, (None, "terminated", None))[2],
                depth=depth_found,
            )
            for agent, depth_found, _score in chain
        ]
    )
