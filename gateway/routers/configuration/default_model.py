from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from base.agents.model.birth_config import resolve_default_model, set_cluster_default_model
from gateway.http.model_catalog import require_model_catalog
from gateway.schemas.models import DefaultModelView, DefaultModelWrite

router = APIRouter()


@router.get(
    "/api/config/default-model",
    description="The model a new agent is born on, and where that value came from.",
)
def get_default_model(request: Request) -> DefaultModelView:

    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        resolved = resolve_default_model(
            cur,
            catalog=require_model_catalog(request),
            authority=request.app.state.config_authority,
        )
        return DefaultModelView(model=resolved.model, source=resolved.source)


@router.put(
    "/api/config/default-model",
    description="Set the cluster's default model.\n\n400 when the id is not a spawnable model in the model catalog.\nTakes effect for agents born after the write; every existing agent keeps the\nmodel stamped on its own row.",
)
def put_default_model(body: DefaultModelWrite, request: Request) -> DefaultModelView:

    catalog = require_model_catalog(request)
    spawnable = {m for models in catalog.supported_models.values() for m in models}
    if body.model not in spawnable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown model {body.model!r}; pick one of {sorted(spawnable)} "
                f"(the spawnable roster in base/lm/registry.py)"
            ),
        )
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        set_cluster_default_model(cur, body.model, updated_by="api")
        resolved = resolve_default_model(
            cur, catalog=catalog, authority=request.app.state.config_authority
        )
        return DefaultModelView(model=resolved.model, source=resolved.source)
