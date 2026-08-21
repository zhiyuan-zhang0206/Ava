"""LLM model catalog (GET /api/models) + the cluster default model
(GET/PUT /api/config/default-model).

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from typing import Literal

from pydantic import (
    BaseModel,
)


class ModelPricing(BaseModel):
    """Per-million-token pricing for one model (USD)."""

    input: float  # cache-miss input rate
    cache_read: float  # cached input rate
    output: float  # output rate


class ModelInfo(BaseModel):
    """Per-model metadata surfaced in the spawn picker."""

    provider: str
    context_window: int
    pricing: ModelPricing | None = None
    reasoning_effort_options: list[str] | None = None
    # The model's concrete default reasoning effort — the level a spawn with no
    # explicit reasoning_effort runs at, and what the picker pre-selects.
    # None = no concrete default (effective default is the provider's own; the
    # picker then keeps a synthetic "provider default" option).
    reasoning_effort_default: str | None = None
    # The model id that replaced this one in the spawn picker, or None when the
    # model still shows. Display-only: a superseded model remains spawnable and
    # config-valid; the frontend filters it from the picker by this field.
    superseded_by: str | None = None


class ModelsResponse(BaseModel):
    """GET /api/models — model picker source for the spawn UI."""

    providers: dict[str, list[str]]
    models: dict[str, ModelInfo]
    default: str


class DefaultModelView(BaseModel):
    """GET/PUT /api/config/default-model — the model a new agent is born on.

    `source` names where `model` came from, because the two cases behave
    differently on the next `.env` edit: `cluster` is the DB row (a deliberate
    choice through this endpoint, which outranks `.env`), `config` is the ordinary
    chain (`.env` AVA_MODEL, else the code default) showing through because no
    cluster choice has been made.
    """

    model: str
    source: Literal["cluster", "config"]


class DefaultModelWrite(BaseModel):
    """PUT /api/config/default-model body. `model` must be a spawnable id from the
    registry roster; anything else is rejected rather than stored."""

    model: str
