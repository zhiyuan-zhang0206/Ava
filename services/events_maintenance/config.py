"""Configuration slice of the events-maintenance daemon.

Fields keep their flat registry names. `services/events_maintenance/daemon.py` (the
composition root) is the only module of the package that reads `settings`; it builds
this slice and hands it to the loops and passes. See `future/infra/dependency-injection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class EventsMaintenanceConfig:
    events_maintenance_interval_seconds: float
    events_maintenance_pass_deadline_s: float
    events_rollup_pass_deadline_s: float
    events_maintenance_resolution_deadline_s: float
    events_rollup_late_write_lookback_days: int
    events_resolution_burst_threshold: int
    events_resolution_interval_seconds: int
    events_auto_dismiss_enabled: bool
    events_auto_dismiss_days: int
    # Shared with the Loki readers and the clock: read by this package too.
    telemetry_loki_url: str
    timezone: str
    # The co-located Grafana the alert reconciliation reads; no admin password means
    # this unit does not reconcile alerts.
    grafana_host: str
    grafana_port: int
    grafana_admin_password: SecretStr | None
