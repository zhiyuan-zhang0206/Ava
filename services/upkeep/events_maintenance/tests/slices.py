"""The events-maintenance slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from base.cluster.machine import MachineIdentity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig


def events_maintenance_config(**overrides: Any) -> EventsMaintenanceConfig:
    return replace(daemon.events_maintenance_config(), **overrides)


def events_maintenance_db(*, local_machine: MachineIdentity) -> Database:
    """The handle on the test database (the settings the suite's conftest points at the throwaway cluster)."""
    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(version=version.get, process="events_maintenance_test")
    return daemon.events_maintenance_db(gate=gate, local_host=local_machine.host)
