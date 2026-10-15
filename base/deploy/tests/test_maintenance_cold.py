from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus


@pytest.mark.parametrize("observed", [False, True])
def test_cold_hosted_restart_tails_and_claimed_chat_are_preserved(
    db_conn: psycopg.Connection[Any],
    observed: bool,
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    owner, generation = uuid4(), uuid4()
    chat = insert_inbound_message(
        db_conn, agent, "unfinished action", "user", bus=event_bus, database=database
    )
    restart = insert_inbound_message(
        db_conn, agent, "", "system:update", kind="restart", bus=event_bus, database=database
    )
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE id=%s", (chat,))
    db_conn.execute(
        "UPDATE inbound_messages SET status=%s,applied_at=clock_timestamp(),observed_at=%s,"
        "target_owner=%s,target_generation=%s,claimed_at=clock_timestamp() WHERE id=%s",
        ("done" if observed else "claimed", WHEN if observed else None, owner, generation, restart),
    )
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind=%s,runtime_owner=%s,runtime_generation=%s,"
        "lifecycle_command_id=%s WHERE id=%s",
        (
            "hosted" if observed else None,
            owner if observed else None,
            generation if observed else None,
            None if observed else restart,
            agent,
        ),
    )
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    if not observed:
        with pytest.raises(RuntimeError):
            cohort.prepare(
                db_conn,
                machine=machine_identity.name(),
                host_owner=None,
                holder="cold",
                acquired_at=WHEN,
            )
        db_conn.rollback()
    held = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )
    assert held.parked == (agent,) and held.commands == {} and held.drained == ()
    cohort.verify_drained(db_conn, held)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("claimed",)


def test_expired_host_lease_does_not_prove_normal_shutdown(
    db_conn: psycopg.Connection[Any],
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_owner=%s,runtime_generation=%s,"
        "lease_expires_at=%s WHERE id=%s",
        (uuid4(), uuid4(), WHEN - timedelta(days=1), agent),
    )
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    with pytest.raises(RuntimeError, match="live original native"):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="cold",
            acquired_at=WHEN,
            host_absent=True,
        )


def test_consumer_scan_skips_a_process_whose_cmdline_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.deploy.maintenance import cold

    class _ReadFails:
        pid = 424242

        @property
        def info(self) -> dict[str, object]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

        def cmdline(self) -> list[str]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

    class _Consumer:
        pid = 5005

        def cmdline(self) -> list[str]:
            return ["python", "-m", "services.agent_runner.agent_host.daemon"]

        def environ(self) -> dict[str, str]:
            return {}

    procs: list[object] = [_ReadFails(), _Consumer()]

    def process_iter(*args: object, **kwargs: object) -> Iterator[object]:
        del args, kwargs
        return iter(procs)

    monkeypatch.setattr(cold.psutil, "process_iter", process_iter)
    with pytest.raises(RuntimeError, match="native consumer still exists during cold prepare"):
        cold.require_no_consumer_process()
