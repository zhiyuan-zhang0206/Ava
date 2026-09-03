"""Resource admission on the same metadata lock as runtime ownership.

NULL stays legacy protocol zero, not a known empty set. No producer in this
module creates a birth marker or enables managed mode on an existing row.
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from shared.incarnation_resources import (
    IncarnationResources,
    ResourceBirth,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from shared.runtime_incarnation import RuntimeIncarnation

_LOCK = "SELECT incarnation_resources,runtime_generation,runtime_owner,runtime_kind,pid,started_at,runtime_protocol_version FROM agents_meta WHERE id=%s FOR UPDATE"
_PREDECESSOR = "SELECT i.id FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id WHERE i.agent_id=%s AND i.target_generation=%s AND i.target_owner=%s AND i.applied_at IS NOT NULL AND ((i.kind='restart' AND i.status='claimed' AND m.lifecycle_command_id=i.id) OR (i.kind='terminate' AND i.status='done' AND i.observed_at IS NOT NULL)) LIMIT 1"
_STORE = "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s"


def _next(
    row: tuple[Any, ...], target: RuntimeIncarnation, host: ResourceProcess, *, predecessor: bool
) -> IncarnationResources | None:
    if row[0] is None:
        if row[6] != 0:
            raise ResourceEvidenceError("unknown resource set cannot admit an enabled protocol")
        return None
    state = decode_resources(row[0])
    if isinstance(state, ResourceBirth):
        if any(value is not None for value in row[1:6]):
            raise ResourceEvidenceError("birth marker is not a never-admitted runtime")
    else:
        if (state.generation, state.owner) == (target.generation, target.owner):
            if state.frozen_by is not None:
                raise ResourceEvidenceError("force freezes same-owner admission")
            if state.host_process != host:
                raise ResourceEvidenceError("same owner changed its actual host process")
            return state
        if state.requests or not predecessor:
            raise ResourceEvidenceError(
                "successor lacks complete predecessor resource/lifecycle closure"
            )
    return IncarnationResources(
        generation=target.generation, owner=target.owner, host_process=host, requests={}
    )


def admit_resources(
    conn: psycopg.Connection, target: RuntimeIncarnation, host: ResourceProcess
) -> None:
    row = conn.execute(_LOCK, (target.agent_id,)).fetchone()
    if row is None:
        raise ResourceEvidenceError("resource admission target does not exist")
    predecessor = False
    if row[0] is not None:
        state = decode_resources(row[0])
        if isinstance(state, IncarnationResources):
            predecessor = (
                conn.execute(
                    _PREDECESSOR, (target.agent_id, state.generation, state.owner)
                ).fetchone()
                is not None
            )
    value = _next(row, target, host, predecessor=predecessor)
    if value is not None:
        conn.execute(_STORE, (Jsonb(value.model_dump(mode="json")), target.agent_id))


async def admit_resources_async(
    conn: psycopg.AsyncConnection, target: RuntimeIncarnation, host: ResourceProcess
) -> None:
    row = await (await conn.execute(_LOCK, (target.agent_id,))).fetchone()
    if row is None:
        raise ResourceEvidenceError("resource admission target does not exist")
    predecessor = False
    if row[0] is not None:
        state = decode_resources(row[0])
        if isinstance(state, IncarnationResources):
            predecessor = (
                await (
                    await conn.execute(
                        _PREDECESSOR, (target.agent_id, state.generation, state.owner)
                    )
                ).fetchone()
                is not None
            )
    value = _next(row, target, host, predecessor=predecessor)
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
