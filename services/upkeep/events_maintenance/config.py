"""Configuration slice of the events-maintenance daemon, and what its passes share.

Fields keep their flat registry names. `services/upkeep/events_maintenance/daemon.py` (the
composition root) builds
this slice from the supplied ConfigBoot and hands it to the loops and passes. See `future/infra/security/dependency-injection.md`.

The module also owns the two facts the daemon and the operator commands both need and neither
may import from the other: the database handle built from a boot owner, and the recompute horizon
of the closed-day ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot

# Closed UTC days stay open to recomputation this long (late writes, a backfill); a day older than
# this can no longer change, so the token totals fold it. Late writes arrive up to the mirror's
# seven-day retention after the event; one more day of margin covers a replay that straddles midnight.
RECOMPUTE_DAYS = 8


@dataclass(frozen=True)
class EventsMaintenanceConfig:
    events_maintenance_interval_seconds: float
    events_maintenance_pass_deadline_s: float
    events_maintenance_resolution_deadline_s: float
    events_resolution_burst_threshold: int
    events_resolution_interval_seconds: int
    events_auto_dismiss_enabled: bool
    events_auto_dismiss_days: int
    timezone: str
    # The co-located Grafana the alert reconciliation reads; no admin password means
    # this unit does not reconcile alerts.
    grafana_host: str
    grafana_port: int
    grafana_admin_password: SecretStr | None


def events_maintenance_db(
    config: ConfigBoot, *, gate: ProcessDbGate, local_host: Callable[[], str]
) -> Database:
    """The handle on the cluster database, built where the daemon (or one of its operator
    commands) starts."""
    return Database(db_config_from_boot(config), gate=gate, local_host=local_host)
