"""Host-specific command rendering in lease-expiry reminders."""

import shlex
import sys
from uuid import uuid4

import psycopg
import pytest

from shared.agents import impersonation as leases
from shared.agents.impersonation.impersonation_maintenance import remind_expiring_impersonations
from shared.caller_identity import CallerIdentity
from shared.db import create_agent, pool
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import recorded_tree


@pytest.mark.parametrize(
    ("machine", "home", "stopped_home", "null_home"),
    [
        ("macbook-air", "/Users/owner/.ava", None, None),
        ("ubuntu-runner", "/home/owner/.ava", None, None),
        ("unregistered", None, None, None),
        ("macbook-air", "/Users/live/.ava", "/Users/stopped/.ava", None),
        ("macbook-air", "/Users/live-newest/.ava", None, "/Users/null-uptime/.ava"),
    ],
)
@pytest.mark.parametrize("invoked_python", [None, "/Users/runner/preview source/.venv/bin/python"])
def test_reminder_uses_request_interpreter_or_legacy_machine_home(
    db_conn: psycopg.Connection,
    machine: str,
    home: str | None,
    stopped_home: str | None,
    null_home: str | None,
    invoked_python: str | None,
) -> None:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = leases.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata={
            **recorded_tree(),
            **({"invoked_python": invoked_python} if invoked_python is not None else {}),
        },
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET machine=%s, "
        "expires_at=clock_timestamp()+interval '4 minutes' WHERE id=%s",
        (machine, lease["id"]),
    )
    if stopped_home is not None:
        db_conn.execute(
            "INSERT INTO machine_units(machine_name,home,up_since_at,stopped_at) "
            "VALUES(%s,%s,clock_timestamp()+interval '1 minute',clock_timestamp())",
            (machine, stopped_home),
        )
    if null_home is not None:
        db_conn.execute(
            "INSERT INTO machine_units(machine_name,home,up_since_at) VALUES(%s,%s,NULL)",
            (machine, null_home),
        )
    if home is not None:
        db_conn.execute(
            "INSERT INTO machine_units(machine_name,home,up_since_at) "
            "VALUES(%s,%s,clock_timestamp())",
            (machine, home),
        )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 1
    reminder = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (agent_id,),
    ).fetchone()
    assert reminder is not None
    content: str = reminder[0]
    _assert_reminder_commands(content, invoked_python, home, stopped_home, null_home)


def _assert_reminder_commands(
    content: str,
    invoked_python: str | None,
    home: str | None,
    stopped_home: str | None,
    null_home: str | None,
) -> None:
    python = invoked_python or (
        f"{home}/source/.venv/bin/python" if home else "~/.ava/source/.venv/bin/python"
    )
    prefix = f"{shlex.quote(python) if invoked_python else python} -m cli impersonate"
    assert f"\n{prefix} renew" in content
    assert f"\n{prefix} release" in content
    assert sys.executable not in content
    if stopped_home is not None:
        assert stopped_home not in content
    if null_home is not None:
        assert null_home not in content
