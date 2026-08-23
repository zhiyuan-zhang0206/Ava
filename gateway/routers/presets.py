"""Config-preset CRUD — /api/presets.

A preset is a named, reusable bundle of per-agent config fields (the same flat
overlay a spawn already accepts: `llm_model`, plugin `per_agent` fields, ...).
Selecting a preset at spawn time seeds the new agent's config from it; an
explicit config passed alongside wins per-key (explicit beats template). The
merge happens in the spawn handler (routers/agents.py), not here.

`config` is stored and returned as an opaque JSONB object for plugin fields: the
gateway process does not load the plugin registry, so it cannot resolve plugin
field names. Framework Settings keys are validated at this write boundary,
however: a known field must be `per_agent=True`; an unknown key remains opaque
for a plugin to resolve when the spawned process applies the overlay.

Hand-writing a preset's `config` JSON is not a UI task — the field names and
skill combinations that make a good preset live in ava-guide.presets, not in
a frontend form. So creation is natural-language: the frontend composes a
short prompt pointing a new agent at `ava.skills.ava_guide.presets` and
spawns it via the plain `POST /api/agents` (see `ui/web/src/app/control/
presets/page.tsx`) — no dedicated backend endpoint; the management page here
only lists / edits label + description / deletes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, LiteralString, cast

import psycopg
from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

router = APIRouter()

_COLS = "id, name, label, description, config, created_at, updated_at"

# Fields a PATCH may set, in the order they map to columns.
_UPDATABLE = ("name", "label", "description", "config")


class PresetView(BaseModel):
    id: int
    name: str
    label: str
    description: str | None
    config: dict[str, object]
    created_at: datetime
    updated_at: datetime


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    description: str | None = None
    config: dict[str, object] = Field(default_factory=dict)


class PresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    label: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    config: dict[str, object] | None = None


def _validate_config_keys(config: dict[str, object]) -> None:
    """Reject known framework keys that cannot vary by agent.

    Unknown names remain valid here because they may belong to a plugin, whose
    schema only the agent process loads.
    """
    from shared.config import field_names, per_agent_field_names

    framework_fields = field_names()
    per_agent_fields = per_agent_field_names()
    for key in config:
        if key in framework_fields and key not in per_agent_fields:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"preset config key {key!r} is a framework Settings field but is not "
                    "per_agent=True; cluster-consistent fields cannot be overridden per agent "
                    "— see shared/plugin_config_registry.py"
                ),
            )


def _view(r: tuple[Any, ...]) -> PresetView:
    return PresetView(
        id=r[0],
        name=r[1],
        label=r[2],
        description=r[3],
        config=r[4],
        created_at=r[5],
        updated_at=r[6],
    )


def _list_blocking(pool: ConnectionPool) -> list[PresetView]:
    """Sync list query — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM agent_presets ORDER BY name")  # noqa: S608 — _COLS is a fixed literal
        return [_view(r) for r in cur.fetchall()]


@router.get("/api/presets")
async def list_presets(request: Request) -> list[PresetView]:
    """List all presets, ordered by name."""
    return await asyncio.to_thread(_list_blocking, request.app.state.db_pool)


def _create_blocking(pool: ConnectionPool, body: PresetCreate) -> tuple[Any, ...]:
    """Sync create INSERT — via to_thread (409 on name clash)."""
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO agent_presets (name, label, description, config) "  # noqa: S608 — _COLS is a fixed literal
                f"VALUES (%s, %s, %s, %s) RETURNING {_COLS}",
                (body.name, body.label, body.description, json.dumps(body.config)),
            )
            row = cur.fetchone()
            assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields a row
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"preset named {body.name!r} already exists"
        ) from exc
    return row


@router.post("/api/presets", status_code=201)
async def create_preset(request: Request, body: PresetCreate) -> PresetView:
    """Create a preset. 409 on a name clash."""
    _validate_config_keys(body.config)
    row = await asyncio.to_thread(_create_blocking, request.app.state.db_pool, body)
    return _view(row)


@router.get("/api/presets/{preset_id}")
async def get_preset(request: Request, preset_id: int) -> PresetView:
    """Get one preset. 404 if it does not exist."""
    row = await asyncio.to_thread(_fetch_blocking, request.app.state.db_pool, preset_id)
    return _view(row)


def _update_blocking(
    pool: ConnectionPool, preset_id: int, fields: dict[str, Any]
) -> tuple[Any, ...]:
    """Sync partial-update — via to_thread (404 missing / 409 name clash)."""
    set_parts = [f"{col} = %s" for col in _UPDATABLE if col in fields]
    values = [
        json.dumps(fields[col]) if col == "config" else fields[col]
        for col in _UPDATABLE
        if col in fields
    ]
    set_parts.append("updated_at = now()")

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                cast(
                    LiteralString,
                    # set_parts comes from the _UPDATABLE whitelist (no user input)
                    f"UPDATE agent_presets SET {', '.join(set_parts)} "  # noqa: S608 — set_parts from the _UPDATABLE whitelist
                    f"WHERE id = %s RETURNING {_COLS}",
                ),
                (*values, preset_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"preset {preset_id} not found")
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"preset named {fields['name']!r} already exists"
        ) from exc
    return row


@router.patch("/api/presets/{preset_id}")
async def update_preset(request: Request, preset_id: int, body: PresetUpdate) -> PresetView:
    """Partial-update a preset — only the fields present in the body change. 404
    if missing, 409 on a name clash."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    config = fields.get("config")
    if config is not None:
        _validate_config_keys(cast(dict[str, object], config))
    row = await asyncio.to_thread(_update_blocking, request.app.state.db_pool, preset_id, fields)
    return _view(row)


def _delete_blocking(pool: ConnectionPool, preset_id: int) -> None:
    """Sync delete + 404 guard — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent_presets WHERE id = %s RETURNING id", (preset_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"preset {preset_id} not found")


@router.delete("/api/presets/{preset_id}")
async def delete_preset(request: Request, preset_id: int) -> dict[str, str]:
    """Delete a preset. 404 if missing."""
    await asyncio.to_thread(_delete_blocking, request.app.state.db_pool, preset_id)
    return {"status": "deleted"}


def _fetch_blocking(pool: ConnectionPool, preset_id: int) -> tuple[Any, ...]:
    """Sync full-row fetch + 404 guard — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM agent_presets WHERE id = %s",  # noqa: S608 — _COLS is a fixed literal
            (preset_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"preset {preset_id} not found")
    return row
