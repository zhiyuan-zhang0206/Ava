"""Explicit machine owners; each consumer supplies its fixture to its real root."""

from typing import Protocol

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot

__all__ = ["MachineIdentitySetter", "machine_identity", "runner_unit", "set_machine_identity"]


class MachineIdentitySetter(Protocol):
    """A test's own identity, returned so its consumer can borrow it explicitly."""

    def __call__(self, *, role: str, name: str | None = None) -> MachineIdentity: ...


@pytest.fixture
def machine_identity(process_config: ConfigBoot) -> MachineIdentity:
    """Own independent field caches; construction does not resolve configuration."""
    return machine_from_boot(process_config)


@pytest.fixture
def set_machine_identity(machine_identity: MachineIdentity) -> MachineIdentitySetter:
    """Set this test's owner and return it; other roots keep their own identities."""

    def configure(*, role: str, name: str | None = None) -> MachineIdentity:
        if name is None:
            machine_identity.set(role=role)
        else:
            machine_identity.set(role=role, name=name)
        return machine_identity

    return configure


@pytest.fixture
def runner_unit(db_conn: psycopg.Connection, machine_identity: MachineIdentity) -> MachineIdentity:
    """Supply an agent-runner owner after the database fixture's per-test cleanup."""
    _ = db_conn
    machine_identity.set(role="agent-runner")
    return machine_identity
