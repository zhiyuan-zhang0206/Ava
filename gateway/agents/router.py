from __future__ import annotations

import asyncio
from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.birth.creation_identity import CreationConflictError
from base.agents.impersonation.manifest import record_central_event
from base.agents.labels import publish_label_updated
from base.agents.observation import roster
from base.agents.observation import snapshot as snapshot_module
from base.api_contracts.agent_contract import AgentNotFound
from base.db.transaction import write_transaction
from base.telemetry.audit_events import prepare_event_log, record_audit
from gateway.agents.creation import create_and_launch_agent, scoped_creation_key
from gateway.agents.schemas import AgentRow, LabelPatchRequest
from gateway.http.auth.request_principal import PRINCIPAL_SCOPE, SCOPE_HEADER
from gateway.http.model_catalog import catalog_for_observation, require_model_catalog
from gateway.inspect import neighbors
from gateway.inspect.schemas import BornChainResponse, BornChainRow
from gateway.schemas.models import ModelsResponse
from ops.rpc_schemas import (
    SpawnAgentRequest,
    SpawnedAgent,
)

router = APIRouter()


@router.patch(
    "/api/agents/{agent_id}",
    status_code=204,
    description='Manually set / reset an agent label.\n\nbody.label non-empty: UPDATE directly. Empty string (after strip):\nUPDATE label=NULL to reset back to "not set"; frontend re-displays\nfallback `#N`. Both cases also set `label_user_set=TRUE` — sticky bit\nso the background LLM CAS no longer overwrites (otherwise after the\nuser resets, the LLM would still match `label IS NULL` and rename it,\ndefeating the reset intent). The change and its `label_change` audit fact commit\ntogether; both cases publish LabelUpdated so SSE pushes in real time.\n\n404: agent_id does not exist.',
)
async def patch_agent(agent_id: int, body: LabelPatchRequest, request: Request) -> Response:

    new_label: str | None = body.label if body.label else None
    await asyncio.to_thread(
        _patch_label_blocking, request.app.state.db_pool, agent_id, new_label, body.source
    )
    await publish_label_updated(request.app.state.bus, agent_id, new_label)
    return Response(status_code=204)


@router.get(
    "/api/models",
    description="List selectable LLM models (grouped by provider) + the cluster default.\n\nRoster and tuning come from the model registry; current rates come from\nthe versioned pricing catalog. The default uses the same DB/config\nresolution as the birth stamp and spawn preflight.",
)
def get_models(request: Request) -> ModelsResponse:

    from base.agents.model.birth_config import resolve_default_model
    from base.lm.pricing import rates_at
    from base.lm.registry import explain_setting
    from gateway.schemas.models import ModelInfo, ModelPricing

    catalog = require_model_catalog(request)

    models: dict[str, ModelInfo] = {}
    for provider, model_list in catalog.supported_models.items():
        for model in model_list:
            spec = catalog.models[model]
            rates = rates_at(model, input_tokens=0, prices=catalog.prices)
            if rates is None:
                raise RuntimeError(f"spawnable model {model!r} has no current catalog price")
            pricing = ModelPricing(
                input=rates.cache_miss,
                cache_read=rates.cache_hit,
                output=rates.output,
                cache_write_5m=rates.cache_write_5m,
                cache_write_1h=rates.cache_write_1h,
            )

            resolved_effort = explain_setting(
                "reasoning_effort", model=model, explicit=None, models=catalog.models
            ).value
            models[model] = ModelInfo(
                provider=provider,
                context_window=spec.context_window or 0,
                pricing=pricing,
                reference_tps=spec.reference_tps,
                fast_of=spec.fast_of,
                reasoning_effort_options=(
                    list(spec.effort_levels) if spec.effort_levels is not None else None
                ),
                reasoning_effort_default=resolved_effort or None,
                superseded_by=spec.superseded_by,
            )

    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        default_model = resolve_default_model(
            cur, catalog=catalog, authority=request.app.state.config_authority
        ).model
    return ModelsResponse(
        providers=dict(catalog.supported_models),
        models=models,
        default=default_model,
    )


@router.get(
    "/api/agents",
    description="Read one directory page. History is explicit and never fetched implicitly.",
)
def get_agents(
    request: Request,
    scope: Annotated[roster.AgentDirectoryScope, Query()] = "live",
    query: Annotated[str, Query(max_length=200)] = "",
    before_id: Annotated[int | None, Query(gt=0, le=9223372036854775807)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> roster.AgentDirectoryPage:

    with request.app.state.db_pool.connection() as conn:
        return roster.list_directory(
            conn,
            catalog=catalog_for_observation(request.app.state.catalog),
            scope=scope,
            query=query,
            before_id=before_id,
            limit=limit,
            default_model_reader=lambda: cast(
                str, request.app.state.config_authority.service_field_value("llm_model")
            ),
        )


@router.get(
    "/api/agents/roster",
    description="Read the live tree and its necessary ancestor links in one snapshot.",
)
def get_agent_roster(request: Request) -> roster.AgentRoster:

    with request.app.state.db_pool.connection() as conn:
        return roster.select_roster(
            conn,
            catalog=catalog_for_observation(request.app.state.catalog),
            default_model_reader=lambda: cast(
                str, request.app.state.config_authority.service_field_value("llm_model")
            ),
        )


def _patch_label_blocking(
    pool: ConnectionPool, agent_id: int, new_label: str | None, source: str
) -> None:

    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label=%s, label_user_set=TRUE WHERE id=%s",
                (new_label, agent_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        label_event = record_audit(
            conn,
            record_central_event(
                conn,
                prepare_event_log(
                    event_type="label_change",
                    agent_id=agent_id,
                    source=f"agent:{agent_id}" if source == "self" else source,
                    payload={"new_label": new_label},
                ),
            ),
        )
    telemetry.emit_prepared(label_event)


@router.post(
    "/api/keyed/v1/agents",
    status_code=201,
    response_model_exclude_none=True,
    description="Create or fork an agent through principal-bound keyed admission.\n\nSpawn is HTTP-uniform: every spawn funnels through\n`create_and_launch_agent`, which creates the agent row in the gateway\nprocess (the main data-plane identity — the target runner's ops server\nruns as the least-privilege `ava_runner` role and cannot INSERT agents)\nand forwards a launch-only op to the target runner's ops server, whether\nthe runner is remote or co-located on this box (localhost) — one code\npath, uniform logs/traces, no in-process shortcut. Auto label generation\nis done asynchronously by the services/derived/labeler daemon — does not block\nthe spawn response.\n\nPlain and fork first prompts commit with the row. The fork marker precedes\nits chat in that transaction. InboundArrived is a best-effort live hint;\nthe pending scan recovers a missed wake.\n\nbody.machine = None targets the local machine (which must be a registered\nagent-runner).\n\nCallers must keep the key fixed for one intent: a retry replays the retained\nbirth instead of creating another agent.\n\n400: the target machine is registered but has no agent-runner capability\n(wire `reason='spawn_target_not_agent_runner'`). 404: the target is not in the\nregistry. 409: the fork_from agent has no checkpoint (no LLM/exec step yet),\nor the key already identifies a different request. 422: the scope header is\nnot principal-v1.",
)
async def post_guarded_agents(
    body: SpawnAgentRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> SpawnedAgent:

    if idempotency_scope != PRINCIPAL_SCOPE:
        raise HTTPException(status_code=422, detail="guarded creation requires principal-v1 scope")
    key = scoped_creation_key(request, idempotency_key, operation_path="/api/keyed/v1/agents")
    target = body.machine if body.machine is not None else request.app.state.machine.name()
    try:
        return await create_and_launch_agent(
            body,
            target,
            request.app.state.db_pool,
            request.app.state.db,
            request.app.state.bus,
            catalog=require_model_catalog(request),
            authority=request.app.state.config_authority,
            creation_key=key,
        )
    except CreationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/api/agents/{agent_id}",
    description="Full detail addressed by ID, including terminated agents outside loaded pages.\n\nThe browser and SDK status readers use this independent selection boundary.\nNotice bodies stay here and in the Inbox; directory cards contain counts.\nA nonexistent ID returns 404 rather than falling back to another agent.",
)
def get_agent(agent_id: int, request: Request) -> AgentRow:

    with request.app.state.db_pool.connection() as conn:
        snap = snapshot_module.select_one(
            conn,
            agent_id,
            catalog=catalog_for_observation(request.app.state.catalog),
            default_model_reader=lambda: cast(
                str, request.app.state.config_authority.service_field_value("llm_model")
            ),
        )
    if snap is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return AgentRow.model_validate(snap.model_dump())


@router.get(
    "/api/agents/{agent_id}/born-chain",
    description="The immutable birth chain above `agent_id`, nearest ancestor first\n(1 = direct birth parent) — one recursive `agents_meta.born_spawner` walk.\n\nThe light half of `/neighbors`, and separate on purpose: the inherited\nmemory context note resolves this chain at every window establishment, and\nit must not drag the tie graph's frozen-archive read + Loki live tail into\nthat path. Adds `machine` (which ancestors a reader can open locally) and\nkeeps `status` (the chain includes terminated ancestors). Terminates only\nwhen `born_spawner` is not an `agent:N` value (a user / external spawn).\n\n404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).",
)
def get_agent_born_chain(agent_id: int, request: Request) -> BornChainResponse:

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
