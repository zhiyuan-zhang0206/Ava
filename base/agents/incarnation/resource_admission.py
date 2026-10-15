"""Resource admission on the same metadata lock as runtime ownership.

NULL stays legacy protocol zero, not a known empty set. No producer in this
module creates a birth marker or enables managed mode on an existing row.
Runtime ownership is the handoff authority. Once the metadata row releases
its owner, an empty set can be reused without reconstructing lifecycle history.
An unreleased owner still requires positive evidence that its host exited.
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceBirth,
    ResourceClosureError,
    ResourceEvidenceError,
    ResourceFenceError,
    ResourceProcess,
    decode_resources,
)
from base.native_process.runtime_incarnation import RuntimeIncarnation

_LOCK = "SELECT incarnation_resources,runtime_generation,runtime_owner,runtime_kind,pid,started_at FROM agents_meta WHERE id=%s FOR UPDATE"
_STORE = "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s"
# What a drained restart leaves: protocol-zero NULL, or the complete recorded
# set of exactly the incarnation restart `i` released, empty and unfrozen.
DRAINED_RESOURCES = (
    "(m.incarnation_resources IS NULL OR (m.incarnation_resources->>'state'='admitted' "
    "AND m.incarnation_resources->>'generation'=i.target_generation::text "
    "AND m.incarnation_resources->>'owner'=i.target_owner::text "
    "AND m.incarnation_resources->>'frozen_by' IS NULL "
    "AND m.incarnation_resources->'requests'='{}'::jsonb))"
)


def _require_same_host(state: IncarnationResources, host: ResourceProcess) -> None:
    if state.frozen_by is not None:
        raise ResourceFenceError("force freezes same-owner admission")
    if state.host_process is None or not state.host_process.same_birth(host):
        raise ResourceFenceError("same owner changed its actual host process")


def next_incarnation_resources(
    row: tuple[Any, ...],
    target: RuntimeIncarnation,
    host: ResourceProcess,
    *,
    exited_predecessor: ResourceProcess | None = None,
) -> IncarnationResources | None:
    if row[0] is None:
        return None
    state = decode_resources(row[0])
    if isinstance(state, ResourceBirth):
        if any(value is not None for value in row[1:6]):
            raise ResourceEvidenceError("birth marker is not a never-admitted runtime")
    else:
        if (state.generation, state.owner) == (target.generation, target.owner):
            _require_same_host(state, host)
            return state
        released = all(value is None for value in row[1:5])
        exited_host = (
            state.frozen_by is None
            and state.host_process is not None
            and state.host_process == exited_predecessor
        )
        if state.requests or not (released or exited_host):
            raise ResourceClosureError(
                "successor requires released ownership and closed exec resources",
                receipt_less=bool(state.requests),
            )
    return IncarnationResources(
        generation=target.generation, owner=target.owner, host_process=host, requests={}
    )


async def admit_resources_async(
    conn: psycopg.AsyncConnection,
    target: RuntimeIncarnation,
    host: ResourceProcess,
    *,
    exited_predecessor: ResourceProcess | None = None,
) -> None:
    row = await (await conn.execute(_LOCK, (target.agent_id,))).fetchone()
    if row is None:
        raise ResourceEvidenceError("resource admission target does not exist")
    value = next_incarnation_resources(row, target, host, exited_predecessor=exited_predecessor)
    if value is not None:
        await conn.execute(_STORE, (Jsonb(value.model_dump(mode="json")), target.agent_id))


async def require_resources_closed_async(conn: psycopg.AsyncConnection, agent_id: int) -> None:
    row = await (
        await conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
        )
    ).fetchone()
    if row is None:
        raise ResourceEvidenceError("resource target vanished")
    if row[0] is not None:
        state = decode_resources(row[0])
        if not isinstance(state, IncarnationResources) or state.requests:
            raise ResourceEvidenceError("managed exec resources remain unresolved")
