"""The cluster's default model — GET/PUT /api/config/default-model.

The one value in `cluster_defaults`: which model a NEW agent is born on. It is a
spawn-time input, not a config layer — nothing reads it into `settings`, and no
running process consults it for its own behavior (see `shared/birth_config.py`).
An agent already alive carries its own frozen choice on its row, so editing this
never moves anyone who already exists.

Deliberately its own narrow endpoint rather than a field on `PUT /api/config`.
That PUT is a reducer over the whole editable `.env` surface and has a history of
a partial payload unsetting everything it did not name; a one-value write with a
roster check has no business sharing that machinery — and this value does not live
in `.env` at all.

GET reports the effective default plus which layer produced it. PUT validates
against the registry's spawnable roster (the same roster the spawn picker renders)
and 400s on anything else, so an unknown id can never be stored and then blow up
at the far-away spawn.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from gateway.schemas import DefaultModelView, DefaultModelWrite
from shared.birth_config import cluster_default_model, set_cluster_default_model
from shared.config import settings

router = APIRouter()


def _view(stored: str | None) -> DefaultModelView:
    """The effective default: the cluster row when set, else the ordinary config
    chain showing through."""
    if stored is not None:
        return DefaultModelView(model=stored, source="cluster")
    return DefaultModelView(model=settings.lm.llm_model, source="config")


@router.get("/api/config/default-model")
def get_default_model(request: Request) -> DefaultModelView:
    """The model a new agent is born on, and where that value came from."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        return _view(cluster_default_model(cur))


@router.put("/api/config/default-model")
def put_default_model(body: DefaultModelWrite, request: Request) -> DefaultModelView:
    """Set the cluster's default model.

    400 when the id is not a spawnable model in `shared/lm/registry.py:MODELS`.
    Takes effect for agents born after the write; every existing agent keeps the
    model stamped on its own row.
    """
    from shared.lm.factory import SUPPORTED_MODELS

    spawnable = {m for models in SUPPORTED_MODELS.values() for m in models}
    if body.model not in spawnable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown model {body.model!r}; pick one of {sorted(spawnable)} "
                f"(the spawnable roster in shared/lm/registry.py)"
            ),
        )
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        set_cluster_default_model(cur, body.model, updated_by="api")
        return _view(cluster_default_model(cur))
