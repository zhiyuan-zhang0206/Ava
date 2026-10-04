"""The events-maintenance slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from base.db import Database
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig


def events_maintenance_config(**overrides: Any) -> EventsMaintenanceConfig:
    return replace(daemon.events_maintenance_config(), **overrides)


def events_maintenance_db() -> Database:
    """The handle on the test database (the settings the suite's conftest points at the throwaway cluster)."""
    return daemon.events_maintenance_db()
