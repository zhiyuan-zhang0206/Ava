# meiva:note
# The pooler's client cap is configuration, not a module constant: one fixed 500 sat
# below the fleet's summed client-pool maxima and ran out under a single burst
# (2026-10-17 19:17), so rendering must emit the configured value.

from __future__ import annotations

from base.cluster.machine import MachineIdentity
from cli.commands.data_plane import pgbouncer as pooler
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_render_ini_carries_the_configured_client_cap(
    *,
    command_machine: MachineIdentity,
) -> None:
    ini = pooler.render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        machine=command_machine,
        max_client_conn=2000,
    )
    assert "max_client_conn = 2000" in ini.splitlines()
