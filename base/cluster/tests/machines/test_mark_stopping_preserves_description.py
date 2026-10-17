from __future__ import annotations

import psycopg

from base.cluster import machines
from base.cluster.tests.machines.setup import (
    MachineSetup,
    machines_test_db,
    read_description,
    read_machine,
    read_unit_up_since,
    read_up_since,
)
from base.cluster.tests.machines.setup import (
    machine_setup as machine_setup,
)
from base.cluster.tests.machines.setup import (
    truncate_machines as truncate_machines,
)
from base.config import settings
from base.db.code_version_gate import ProcessDbGate


def test_mark_stopping_preserves_description(
    machine_setup: MachineSetup, *, database_gate: ProcessDbGate
) -> None:

    machine_setup(name="keepdesc", role="agent-runner", home="~/.meiva")
    machine_setup.machine.set(description="keep me")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://k:9000")
    machines.mark_stopping(machines_test_db(database_gate=database_gate), "keepdesc", "~/.meiva")
    assert read_description("keepdesc") == "keep me"


def test_register_self_stamps_up_since_on_unit_and_composed_row(
    machine_setup: MachineSetup, *, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="stamp-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://stamp-host:9100"
    )

    (unit_up_since,) = read_unit_up_since("stamp-host", "~/.meiva")
    (composed_up_since,) = read_up_since("stamp-host")
    assert unit_up_since is not None
    assert composed_up_since == unit_up_since


def test_composed_up_since_is_the_max_over_live_units(
    machine_setup: MachineSetup, *, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="max-host", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://max-host:8000"
    )
    machine_setup(name="max-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )

    (gateway_unit,) = read_unit_up_since("max-host", "~/.meiva_gateway")
    (runner_unit,) = read_unit_up_since("max-host", "~/.meiva")
    (composed,) = read_up_since("max-host")
    assert composed == max(gateway_unit, runner_unit)


def test_recompute_tolerates_a_unit_with_null_up_since(
    machine_setup: MachineSetup, *, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="skew-host", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://skew-host:8000"
    )
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET up_since_at = NULL WHERE machine_name = %s AND home = %s",
            ("skew-host", "~/.meiva_gateway"),
        )
        conn.commit()

    machine_setup(name="skew-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )

    (runner_unit,) = read_unit_up_since("skew-host", "~/.meiva")
    (composed,) = read_up_since("skew-host")
    assert composed == runner_unit
    _gateway_url, role, _desc, _stopped = read_machine("skew-host")
    assert role == ["agent-runner", "gateway"]
