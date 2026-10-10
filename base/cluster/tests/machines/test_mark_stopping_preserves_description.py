"""Machines cases: mark stopping preserves description."""

from __future__ import annotations

import psycopg

from base.cluster import machines
from base.cluster.tests.test_machines import (
    _db,
    _read_description,
    _read_machine,
    _read_unit_up_since,
    _read_up_since,
)
from base.cluster.tests.test_machines import (
    _machine_setup as _machine_setup,
)
from base.cluster.tests.test_machines import (
    _truncate_machines as _truncate_machines,
)
from base.config import settings
from base.db.code_version_gate import ProcessDbGate


def test_mark_stopping_preserves_description(
    _machine_setup, *, database_gate: ProcessDbGate
) -> None:
    """stop-triggered recompute does not touch description (host-level, only register writes it)."""
    from base.cluster.machine import set_identity

    _machine_setup(name="keepdesc", role="agent-runner", home="~/.ava")
    set_identity(description="keep me")
    machines.register_self(_db(database_gate=database_gate), url="http://k:9000")
    machines.mark_stopping(_db(database_gate=database_gate), "keepdesc", "~/.ava")
    assert _read_description("keepdesc") == "keep me"


def test_register_self_stamps_up_since_on_unit_and_composed_row(
    _machine_setup, *, database_gate: ProcessDbGate
) -> None:
    """register_self stamps the announce time on the unit row, and the recompute
    carries it onto the composed machines row.

    The column is the "up since" the CLI and the status page render — it exists to
    be shown, so what it must survive is exactly this write-then-compose path.
    """
    _machine_setup(name="stamp-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://stamp-host:9100")

    (unit_up_since,) = _read_unit_up_since("stamp-host", "~/.ava")
    (composed_up_since,) = _read_up_since("stamp-host")
    assert unit_up_since is not None
    assert composed_up_since == unit_up_since


def test_composed_up_since_is_the_max_over_live_units(
    _machine_setup, *, database_gate: ProcessDbGate
) -> None:
    """The composed row takes the LATEST announce across a machine's live units.

    Two co-located units announce at different times; the machine has been up
    since the later one, because that is the one whose announcement is still the
    most recent claim about this host.
    """
    _machine_setup(name="max-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://max-host:8000")
    _machine_setup(name="max-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")

    (gateway_unit,) = _read_unit_up_since("max-host", "~/.ava_gateway")
    (runner_unit,) = _read_unit_up_since("max-host", "~/.ava")
    (composed,) = _read_up_since("max-host")
    assert composed == max(gateway_unit, runner_unit)


def test_recompute_tolerates_a_unit_with_null_up_since(
    _machine_setup, *, database_gate: ProcessDbGate
) -> None:
    """A unit row whose `up_since_at` is NULL still composes.

    `up_since_at` is nullable on machine_units (a unit registered before #981
    has no stamp — the expand migration backfilled it from `last_seen_at`, and
    the contract migration dropped that column). The composition takes a max
    across units, so an unhandled NULL there would not degrade the display — it
    would raise and take the whole register_self down.
    """
    _machine_setup(name="skew-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://skew-host:8000")
    # Rewrite that unit the way a pre-#981 writer would have left it.
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET up_since_at = NULL WHERE machine_name = %s AND home = %s",
            ("skew-host", "~/.ava_gateway"),
        )
        conn.commit()

    _machine_setup(name="skew-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")

    (runner_unit,) = _read_unit_up_since("skew-host", "~/.ava")
    (composed,) = _read_up_since("skew-host")
    # The fresh unit's stamp wins the max (the old unit contributes NULL).
    assert composed == runner_unit
    _gateway_url, role, _desc, _stopped = _read_machine("skew-host")
    assert role == ["agent-runner", "gateway"]
