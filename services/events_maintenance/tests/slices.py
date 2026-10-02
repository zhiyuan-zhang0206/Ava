"""The events-maintenance slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.events_maintenance import daemon
from services.events_maintenance.config import EventsMaintenanceConfig


def events_maintenance_config(**overrides: Any) -> EventsMaintenanceConfig:
    return replace(daemon.events_maintenance_config(), **overrides)
