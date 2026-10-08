"""`pooler.clients` against a real PgBouncer in front of a throwaway Postgres: the admin
console's `SHOW CLIENTS`, minus the console's own connection."""

from __future__ import annotations

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict

from base.cluster.dataplane import pooler
from tests._containers import postgres
from tests.components.cli.test_pgbouncer_wire import (
    _SECRET,
    _pgbouncer_available,
    _pgbouncer_in_front,
)

pytestmark = pytest.mark.skipif(
    not _pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
)


def test_lists_the_open_clients_and_leaves_out_the_console() -> None:
    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        info = conninfo_to_dict(pooled)
        port = int(str(info["port"]))
        with (
            connect(pooled, prepare_threshold=None, application_name="probe-a") as first,
            connect(pooled, prepare_threshold=None, application_name="probe-b") as second,
        ):
            first.execute("SELECT 1")
            second.execute("SELECT 1")

            found = pooler.clients(port, _SECRET)

        assert pooler.clients(port, _SECRET) == []

    assert sorted(client.application for client in found) == ["probe-a", "probe-b"]
    assert {client.database for client in found} == {str(info["dbname"])}
    assert {client.user for client in found} == {str(info["user"])}
    assert all(client.address.startswith("127.0.0.1:") for client in found)
    assert "db=" in found[0].describe() and "app=probe-" in found[0].describe()


def test_an_unreadable_console_raises_instead_of_reading_as_no_clients() -> None:
    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        port = int(str(conninfo_to_dict(pooled)["port"]))
        with pytest.raises(Exception, match=r"."):
            pooler.clients(port, "not-the-admin-password")
