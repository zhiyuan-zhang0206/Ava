"""`ava lgtm on|off`: declare or retract this home as an observability station, then restart
the cluster so the desired services take effect.

The switch itself belongs to observability (`observability/lgtm.py`: the station marker, the
backend names); this module is the restart half — it drives `cmd_start`, which only lifecycle
may import — so it lives here, one level above observability, instead of the other way round.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.lifecycle import start
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from services.supervision.healthchecks.lgtm import lgtm_host_marker


def _reconcile(
    *,
    enabled: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    """Change only backend intent; use the same lifecycle as every service."""
    from base.deploy.lifecycle.service_selection import read_selection

    selection = read_selection()
    names = set(selection.names)
    if (selection.mode == "only") == enabled:
        names.update(BACKENDS)
    else:
        names.difference_update(BACKENDS)
    if selection.mode == "only":
        # Turning off an allowlist containing only LGTM leaves an explicit
        # empty desired tree, not the default all-services selection.
        if not names:
            from services.supervision.roster import build_services

            return start.cmd_start(
                disabled_services=tuple(s.session for s in build_services()),
                retained_children=retained_children,
                database_factory=database_factory,
                producer=producer,
                command=command,
            )
        return start.cmd_start(
            only_services=tuple(sorted(names)),
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            command=command,
        )
    return start.cmd_start(
        disabled_services=tuple(sorted(names)),
        all_services=not names,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_on(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    """Declare this home an observability station and reconcile normal start."""
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    marker = lgtm_host_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    return _reconcile(
        enabled=True,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_off(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    """Disable the backend units, preserving observation data and other services."""
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    lgtm_host_marker().unlink(missing_ok=True)
    return _reconcile(
        enabled=False,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )
