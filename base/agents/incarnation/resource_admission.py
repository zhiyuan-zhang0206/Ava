"""Resource admission on the same metadata lock as runtime ownership.

NULL stays legacy protocol zero, not a known empty set. No producer in this
module creates a birth marker or enables managed mode on an existing row.
A successor replaces another incarnation's set only through the predecessor
receipt rule; the closed-predecessor form (the recorded incarnation with no
host identity and an empty set) is admitted by that same rule, with no other
acceptance path.

One release record joins that rule: a resurrection whose ownership flip closed
out a retained hosted incarnation records the released generation and owner
(with `lifecycle_release`) on its own resurrect inbound, in the same
transaction. A successor admits against that record only when the stored set
is closed and unfrozen, the row is unowned, the exact recorded host process is
the admitting one, and the predecessor life left no unsettled
restart/terminate command; the record never admits a different generation or a
foreign host.
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
_PREDECESSOR = f"SELECT 1 FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id WHERE {PREDECESSOR_RECEIPT} LIMIT 1"  # noqa: S608 -- constant SQL fragment

# The release record of a resurrection that closed out a retained incarnation,
# bound to the exact released generation and owner. Valid only while the
# predecessor life carries no unsettled restart/terminate command (applied or
# observed, or settled by the resurrect supersession): those are exactly the
# admission guards the successor re-validates against this record.
_RELEASED = (
    "SELECT 1 FROM inbound_messages r WHERE r.agent_id=%s AND r.kind='resurrect' "
    "AND COALESCE((r.payload -> 'lifecycle_release') = 'true'::jsonb, false) "
    "AND (r.payload -> 'release_target' ->> 'generation') = %s "
    "AND (r.payload -> 'release_target' ->> 'owner') = %s "
    "AND NOT EXISTS (SELECT 1 FROM inbound_messages c WHERE c.agent_id=r.agent_id "
    "AND c.kind IN ('restart','terminate') AND c.id < r.id "
    "AND ((c.status IN ('pending','claimed') AND c.applied_at IS NULL) "
    "OR (c.kind='terminate' AND c.status='claimed' AND c.applied_at IS NOT NULL))) "
    "LIMIT 1"
)
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


def _released_transfer(
    row: tuple[Any, ...],
    state: IncarnationResources,
    host: ResourceProcess,
    *,
    released_predecessor: bool,
) -> bool:
    """The release record transfers only to the same live host process that
    recorded it, on a closed set, over a row with no runtime identity left."""
    return (
        released_predecessor
        and row[1] is None
        and row[2] is None
        and row[3] is None
        and not state.requests
        and state.frozen_by is None
        and state.host_process is not None
        and state.host_process.same_birth(host)
    )


def _dead_empty_transfer(
    state: IncarnationResources, exited_predecessor: ResourceProcess | None
) -> bool:
    return (
        not state.requests
        and state.frozen_by is None
        and state.host_process is not None
        and state.host_process == exited_predecessor
    )


def next_incarnation_resources(
    row: tuple[Any, ...],
    target: RuntimeIncarnation,
    host: ResourceProcess,
    *,
    predecessor: bool,
    released_predecessor: bool = False,
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
        released = _released_transfer(row, state, host, released_predecessor=released_predecessor)
        dead_empty_host = _dead_empty_transfer(state, exited_predecessor)
        if state.requests or not (predecessor or released or dead_empty_host):
            raise ResourceClosureError(
                "successor lacks complete predecessor resource/lifecycle closure",
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
    predecessor = False
    released_predecessor = False
    if row[0] is not None:
        state = decode_resources(row[0])
        if isinstance(state, IncarnationResources):
            receipt = await (
                await conn.execute(_PREDECESSOR, (target.agent_id, state.generation, state.owner))
            ).fetchone()
            predecessor = receipt is not None
            if not predecessor:
                released_predecessor = (
                    await (
                        await conn.execute(
                            _RELEASED,
                            (target.agent_id, str(state.generation), str(state.owner)),
                        )
                    ).fetchone()
                    is not None
                )
    value = next_incarnation_resources(
        row,
        target,
        host,
        predecessor=predecessor,
        released_predecessor=released_predecessor,
        exited_predecessor=exited_predecessor,
    )
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
