"""Registration test roots retain one machine owner and an explicit unit home."""

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.cluster.machines import register_self
from base.config import ConfigBoot, settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate


class MachineSetup:
    def __init__(self, machine: MachineIdentity) -> None:
        self.machine = machine
        self.home = "~/.ava"

    def __call__(self, *, name: str, role: str = "gateway", home: str = "~/.ava") -> None:
        self.machine.set(name=name, role=role)
        self.home = home

    def set_home(self, home: str) -> None:
        self.home = home

    def register(self, database: Database, url: str | None = None) -> None:
        register_self(
            database,
            url=url,
            name=self.machine.name(),
            roles=self.machine.role(),
            description=self.machine.description(),
            home=self.home,
            gateway=settings.gateway.gateway_url.strip() or None,
        )


def machines_test_db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


@pytest.fixture(autouse=True)
def truncate_machines(db_conn: None) -> None:
    """Truncate both tables per test — leftover register_self/recompute across tests would pollute subsequent assertions."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()


@pytest.fixture
def machine_setup(monkeypatch: pytest.MonkeyPatch, process_config: ConfigBoot) -> MachineSetup:
    """Provide one explicit registration owner; no process identity slot is changed."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://localhost:8000")
    return MachineSetup(machine_from_boot(process_config))


def read_machine(name: str) -> tuple:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT gateway_url, role, description, stopped_at FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def read_description(name: str) -> str | None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT description FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def read_up_since(name: str):
    """machines.up_since_at for `name`."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT up_since_at FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row


def read_unit_up_since(name: str, home: str):
    """machine_units.up_since_at for one unit."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT up_since_at FROM machine_units WHERE machine_name = %s AND home = %s",
            (name, home),
        )
        row = cur.fetchone()
    assert row is not None
    return row
