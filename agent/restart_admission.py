"""Bind a delayed restart boot to the command that authorized it."""

import psycopg

from shared.boot_timing import BOOT_BUDGET_SEC


def consume_restart_command(argv: list[str]) -> int | None:
    """Consume the non-secret launch identity before the strict runtime parser."""
    flag = "--restart-command-id"
    if flag not in argv:
        return None
    if argv.count(flag) != 1:
        raise ValueError("restart command flag must occur once")
    index = argv.index(flag)
    command = int(argv[index + 1])
    if command <= 0:
        raise ValueError("restart command id must be positive")
    del argv[index : index + 2]
    return command


def require_restart_admission(
    conn: psycopg.Connection, agent_id: int, command_id: int | None
) -> None:
    """Validate after the caller locks agents_meta, using a fresh database clock.

    The deadline derives from the original durable application time, never the
    current launch attempt. This is not an invitation to start a dead agent:
    only an already-applied restart targeting the retained incarnation admits.
    """
    row = conn.execute(
        "SELECT m.lifecycle_command_id, "
        "i.id=%s AND i.kind='restart' AND i.status='claimed' "
        "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL "
        "AND i.target_generation=m.runtime_generation AND i.target_owner=m.runtime_owner "
        "AND m.runtime_kind='process' "
        "AND clock_timestamp()<i.applied_at+make_interval(secs=>%s), "
        "i.payload->'launch_attempts' "
        "FROM agents_meta m LEFT JOIN inbound_messages i "
        "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id WHERE m.id=%s",
        (command_id, BOOT_BUDGET_SEC, agent_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("restart admission target does not exist")
    if command_id is None and row[0] is None:
        return
    if command_id is None or row[0] != command_id or row[1] is not True:
        raise RuntimeError("restart admission command is stale, unproven, or expired")
    if type(row[2]) is not int or row[2] <= 0:
        raise RuntimeError("restart admission has no committed launch authorization")
