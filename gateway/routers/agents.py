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
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from gateway import neighbors
from gateway.routers.agents_forward import _forward_spawn_to_remote
from gateway.schemas import (
    AgentCompact,
    AgentRow,
    AgentSummary,
    BornChainResponse,
    BornChainRow,
    LabelPatchRequest,
    ModelsResponse,
)
from ops.agent_spawn import create_agent_row
from ops.ops_lifecycle import _spawn_prechecks_blocking
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent
from shared import agent_snapshot
from shared.agents import (
    AgentNotFound,
    ForkConfigChangeNotAllowed,
    InvalidModelConfig,
    SpawnTargetNotAgentRunner,
)
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.labels import publish_label_updated
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
    scope: Annotated[agent_snapshot.AgentListScope, Query()] = "all",
    fields: Annotated[agent_snapshot.AgentListFields, Query()] = "full",
) -> list[AgentRow | AgentSummary | AgentCompact]:
    """List agent snapshots for the requested roster scope.

    ``all`` is the compatibility default for SDK / ops callers.
    Frontend fleet/sidebar readers request ``live`` so Postgres excludes
    terminated history before evaluating the per-agent snapshot lookups, and
    request ``terminated`` alongside it: the sidebar's spawn tree needs the
    terminated rows as lineage joints (an alive child of a terminated parent
    re-parents under the nearest visible ancestor — #312 orphan regression).
    Every scope returns raw spawner / fork-source truth; the show-terminated
    UI toggle only controls rendering, never the fetch.

    ``fields=full`` preserves the historical response. ``fields=summary`` is
    the reduced SQL projection used by roster consumers; ``fields=compact``
    remains as a legacy narrow projection. The CLI renders four fields from the
    summary projection. Detail and SSE keep the full snapshot.
    All scopes remain unpaginated for wire compatibility.
    """
    with request.app.state.db_pool.connection() as conn:
        snapshots = agent_snapshot.select_all(conn, scope=scope, fields=fields)
    if fields == "summary":
        return [AgentSummary.model_validate(s.model_dump()) for s in snapshots]
    if fields == "compact":
        return [AgentCompact.model_validate(s.model_dump()) for s in snapshots]
    return [AgentRow.model_validate(s.model_dump()) for s in snapshots]


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
) -> tuple[str | None, list[str] | None]:
    """Sync spawn preflight — via to_thread: registry capability check, preset
    fold, fork config rule + tail-skills delta, model-config validation (may
    read provider API keys). Returns ``(preset_name, tail_skills)`` for the
    agent row / fork inbound.
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
        # reason-less body trips the SDK's `_raise_from_response` into a confusing
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
    # Validate model config before forwarding — fail fast at the gateway
    # instead of letting the agent process silently hang on a missing API key.
    from shared.lm.factory import validate_model_config

    try:
        validate_model_config(model=settings.lm.llm_model, config=body.config)
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc
    return preset_name, tail_skills


# The overlay key naming a preset, and the only overlay keys a fork may change
# (additions only — see _validate_fork_config).
_PRESET_KEY = "preset"
_FORK_SKILL_KEYS = ("skills_to_inject_into_system_prompt", "skills_to_expand_at_start")


def _normalize_and_resolve_preset(
    pool: ConnectionPool, body: SpawnAgentRequest
) -> tuple[str | None, list[str] | None]:
    """Resolve `config_overlay.preset` (or the legacy top-level `body.preset`)
    into the effective overlay, returning the preset name.

    The preset's stored config is the base; the explicit `body.config` fields
    win per-key (explicit beats template). The resolved map is written back to
    `body.config` WITHOUT the preset key and `body.preset` is cleared, so the
    forwarded spawn carries only a plain config — the runner never sees the
    preset. The name is returned separately: it is stored on the agent row
    (`agents_meta.preset_name`) purely for display, next to the resolved
    overlay.

    400 when both the legacy field and the overlay key are given (ambiguous),
    when the overlay key is not a non-empty string, or when the named preset
    does not exist (a spawn referencing a missing preset is a caller error,
    surfaced up front rather than silently ignored).
    """
    explicit = body.config or {}
    overlay_preset = explicit.get(_PRESET_KEY)
    if body.preset is not None and overlay_preset is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "preset given twice — as the top-level field and as "
                "config_overlay.preset; pass only one"
            ),
        )
    preset_name = body.preset if body.preset is not None else overlay_preset
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
    body.preset = None
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
    forward op (`kind="spawn-launch"`) only launches the detached child and
    delivers the plain-spawn first prompt, both within the runner role.

    Every spawn in the system funnels through this helper (POST /api/agents,
    the guide / packages / schedules draft routers, the MCP tools server), so
    preflight, row creation, and launch stay uniform across entry points.
    """
    preset_name, tail_skills = await asyncio.to_thread(
        _spawn_preflight_blocking, target, body, pool
    )
    # fork_checkpoint resolution stays gateway-side: LangGraph checkpoints are
    # append-only and "latest" drifts under concurrent writes, so the gateway
    # resolves an explicit id before creating the row.
    fork_checkpoint = await asyncio.to_thread(_spawn_prechecks_blocking, body, pool)
    is_fork = body.fork_from is not None
    new_id, birth_config = await asyncio.to_thread(
        create_agent_row,
        spawner=body.spawner,
        fork_from=body.fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=target,
        config=body.config,
        label=body.label,
        preset_name=preset_name,
        fork_tail_skills=tail_skills,
        # A fork inherits the source's full history, so its prompt must reach
        # the agent's FIRST claim batch — create_agent_row delivers it pre-launch
        # (a separate insert, mirroring resurrect). A plain spawn has empty
        # history and idles waiting, so it keeps the post-launch delivery in the
        # launch op, which also carries the InboundArrived live-UI signal.
        prompt=body.prompt if is_fork else None,
        prompt_source=body.prompt_source if is_fork else None,
    )
    launch = LaunchAgentRequest(
        agent_id=new_id,
        config=body.config,
        birth_config=birth_config,
        prompt=body.prompt if not is_fork else None,
        prompt_source=body.prompt_source if not is_fork else None,
        label=body.label,
    )
    # The endpoint response is the launch op's verdict (the launched agent id —
    # equal to new_id in production; the runner answers for the launch).
    return await _forward_spawn_to_remote(target, launch)


@router.post("/api/agents", status_code=201)
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

    Fork prompt delivery: for a fork, the prompt is inserted pre-launch by
    create_agent_row (the forked agent inherits a full history and would
    otherwise start a turn on the inherited task before a post-launch prompt
    landed), with no InboundArrived (the agent's claim emits InboundCommitted
    once it picks the prompt up — same as resurrect); for a plain spawn, the
    launch op delivers the first prompt post-launch and publishes
    InboundArrived so all UIs see the new agent received its first task in
    real time.

    body.machine = None targets the local machine (which must be a registered
    agent-runner).

    400: the target machine is registered but has no agent-runner capability
    (wire `reason='spawn_target_not_agent_runner'`). 404: the target is not in the
    registry. 409: the fork_from agent has no checkpoint (no LLM/exec step yet).
    """
    target = body.machine if body.machine is not None else machine_name()
    return await create_and_launch_agent(body, target, request.app.state.db_pool)


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: int, request: Request) -> AgentRow:
    """Full state of a single agent — spot-check endpoint for frontend / ops.
    Previously also served SDK `get_status` (removed — agents no longer query
    peer status; FleetView uses its own API, self-evo uses `list_agents()`).

    Shares the AgentRow schema (including last_active_at computation) with
    `GET /api/agents` (list all).

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
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
