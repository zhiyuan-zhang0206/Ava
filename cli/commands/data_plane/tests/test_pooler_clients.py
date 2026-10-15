from __future__ import annotations

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict

from base.cluster.dataplane import pooler
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.deploy.tests.containers import postgres
from base.deploy.tests.pgbouncer import (
    SECRET,
    pgbouncer_available,
    pgbouncer_in_front,
)

pytestmark = pytest.mark.skipif(
    not pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
)


def test_lists_the_open_clients_and_leaves_out_the_console(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        info = conninfo_to_dict(pooled)
        port = int(str(info["port"]))
        with (
            connect(pooled, prepare_threshold=None, application_name="probe-a") as first,
            connect(pooled, prepare_threshold=None, application_name="probe-b") as second,
        ):
            first.execute("SELECT 1")
            second.execute("SELECT 1")

            found = pooler.clients(port, SECRET)

        assert pooler.clients(port, SECRET) == []

    assert sorted(client.application for client in found) == ["probe-a", "probe-b"]
    assert {client.database for client in found} == {str(info["dbname"])}
    assert {client.user for client in found} == {str(info["user"])}
    assert all(client.address.startswith("127.0.0.1:") for client in found)
    assert "db=" in found[0].describe() and "app=probe-" in found[0].describe()


def test_an_unreadable_console_raises_instead_of_reading_as_no_clients(
    *, postgres_access: None, machine_identity: MachineIdentity, throwaway_config: ThrowawayConfig
) -> None:
    with (
        postgres(config=throwaway_config, machine_name=machine_identity.name) as pg_url,
        pgbouncer_in_front(pg_url) as pooled,
    ):
        port = int(str(conninfo_to_dict(pooled)["port"]))
        with pytest.raises(Exception, match=r"."):
            pooler.clients(port, "not-the-admin-password")
