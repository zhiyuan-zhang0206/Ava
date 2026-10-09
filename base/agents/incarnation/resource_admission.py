"""Resource admission on the same metadata lock as runtime ownership.

NULL stays legacy protocol zero, not a known empty set. No producer in this
module creates a birth marker or enables managed mode on an existing row.
A successor replaces another incarnation's set only through the predecessor
receipt rule; the closed-predecessor form (the recorded incarnation with no
host identity and an empty set) is admitted by that same rule, with no other
acceptance path.
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from base.agents.incarnation.resource_transfer import ResourceTransferProof, ResourceTransferReason
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceBirth,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from base.native_process.runtime_incarnation import RuntimeIncarnation

_LOCK = "SELECT incarnation_resources,runtime_generation,runtime_owner,runtime_kind,pid,started_at,runtime_protocol_version FROM agents_meta WHERE id=%s FOR UPDATE"
# The one predecessor rule: a settled lifecycle receipt for (agent, generation,
# owner): an applied restart, including one superseded by an unowned force,
# or an applied and observed terminate. Aliases: i = inbound_messages, m = agents_meta.
PREDECESSOR_RECEIPT = (
    "i.agent_id=%s AND i.target_generation=%s AND i.target_owner=%s "
    "AND i.applied_at IS NOT NULL AND ((i.kind='restart' AND "
    "((i.status='claimed' AND m.lifecycle_command_id=i.id) OR "
    "(i.status='done' AND i.payload @> "
    '\'{"lifecycle_release":true,"lifecycle_result":{"outcome":"superseded",'
    '"reason":"force_terminate"}}\'::jsonb))) OR '
    "(i.kind='terminate' AND i.status='done' AND i.observed_at IS NOT NULL))"
)
_PREDECESSOR = f"SELECT i.id,i.kind,i.observed_at FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id WHERE {PREDECESSOR_RECEIPT} LIMIT 1"  # noqa: S608 -- constant SQL fragment
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
        raise ResourceEvidenceError("force freezes same-owner admission")
    if state.host_process is None or not state.host_process.same_birth(host):
        raise ResourceEvidenceError("same owner changed its actual host process")


def _record_transfer(
    state: IncarnationResources,
    target: RuntimeIncarnation,
    host: ResourceProcess,
    predecessor_command_id: int | None,
    *,
    predecessor_observed_terminate: bool,
    transfers: list[ResourceTransferProof] | None,
) -> None:
    """Collect only the actual predecessor evidence allowed by admission."""
    if transfers is not None and (
        state.frozen_by is None
        or (state.frozen_by == predecessor_command_id and predecessor_observed_terminate)
    ):
        reason = (
            ResourceTransferReason.LIFECYCLE_RECEIPT
            if predecessor_command_id is not None
            else ResourceTransferReason.EXACT_HOST_EXIT
        )
        transfers.append(
            ResourceTransferProof(
                agent_id=target.agent_id,
                source_generation=state.generation,
                source_owner=state.owner,
                target_generation=target.generation,
                target_owner=target.owner,
                predecessor_process=state.host_process,
                successor_process=host,
                reason=reason,
                lifecycle_command_id=predecessor_command_id,
            )
        )


def _next(
    row: tuple[Any, ...],
    target: RuntimeIncarnation,
    host: ResourceProcess,
    *,
    predecessor: bool,
    exited_predecessor: ResourceProcess | None = None,
    predecessor_command_id: int | None = None,
    predecessor_observed_terminate: bool = False,
    transfers: list[ResourceTransferProof] | None = None,
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
            _require_same_host(state, host)
            return state
        dead_empty_host = (
            not state.requests
            and state.frozen_by is None
            and state.host_process is not None
            and state.host_process == exited_predecessor
        )
        if state.requests or not (predecessor or dead_empty_host):
            raise ResourceEvidenceError(
                "successor lacks complete predecessor resource/lifecycle closure"
            )
        _record_transfer(
            state,
            target,
            host,
            predecessor_command_id,
            predecessor_observed_terminate=predecessor_observed_terminate,
            transfers=transfers,
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
    conn: psycopg.AsyncConnection,
    target: RuntimeIncarnation,
    host: ResourceProcess,
    *,
    exited_predecessor: ResourceProcess | None = None,
) -> ResourceTransferProof | None:
    row = await (await conn.execute(_LOCK, (target.agent_id,))).fetchone()
    if row is None:
        raise ResourceEvidenceError("resource admission target does not exist")
    predecessor = False
    predecessor_command_id = None
    predecessor_observed_terminate = False
    if row[0] is not None:
        state = decode_resources(row[0])
        if isinstance(state, IncarnationResources):
            receipt = await (
                await conn.execute(_PREDECESSOR, (target.agent_id, state.generation, state.owner))
            ).fetchone()
            predecessor = receipt is not None
            predecessor_command_id = None if receipt is None else receipt[0]
            predecessor_observed_terminate = (
                receipt is not None and receipt[1] == "terminate" and receipt[2] is not None
            )
    transfers: list[ResourceTransferProof] = []
    value = _next(
        row,
        target,
        host,
        predecessor=predecessor,
        exited_predecessor=exited_predecessor,
        predecessor_command_id=predecessor_command_id,
        predecessor_observed_terminate=predecessor_observed_terminate,
        transfers=transfers,
    )
    if value is not None:
        await conn.execute(_STORE, (Jsonb(value.model_dump(mode="json")), target.agent_id))
    return transfers[0] if transfers else None


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
