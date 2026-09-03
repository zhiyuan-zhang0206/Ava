"""Exact bounded first-birth tokens; metadata remains their sole authority."""

from uuid import UUID

import psycopg

from shared.incarnation_resources import ResourceBirth, ResourceEvidenceError, decode_resources


def consume_birth_token(argv: list[str]) -> tuple[UUID, int] | None:
    flag = "--resource-birth"
    if flag not in argv:
        return None
    if argv.count(flag) != 1:
        raise ValueError("resource birth flag cannot repeat")
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise ValueError("resource birth token is missing")
    value = argv[index + 1].split(":")
    if len(value) != 2 or int(value[1]) <= 0:
        raise ValueError("invalid resource birth token")
    token = UUID(value[0]), int(value[1])
    del argv[index : index + 2]
    return token


def require_birth_token(
    conn: psycopg.Connection, agent_id: int, token: tuple[UUID, int] | None
) -> None:
    row = conn.execute(
        "SELECT incarnation_resources,status,runtime_generation,runtime_owner,pid "
        "FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise ResourceEvidenceError("birth target is absent")
    state = decode_resources(row[0]) if row[0] is not None else None
    if not isinstance(state, ResourceBirth):
        if token is not None:
            raise ResourceEvidenceError("old birth token cannot claim a successor")
        return
    now = conn.execute("SELECT clock_timestamp()").fetchone()
    if (
        token != (state.birth, state.launch_attempts)
        or state.launch_attempts <= 0
        or state.launch_deadline is None
        or now is None
        or now[0] >= state.launch_deadline
        or row[1:] != ("idling", None, None, None)
    ):
        raise ResourceEvidenceError("first birth allocation is missing, superseded or expired")
