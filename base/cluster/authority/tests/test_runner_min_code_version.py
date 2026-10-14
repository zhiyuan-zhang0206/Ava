"""A runner login reads the code-version minimum and cannot write it.

Split out of `base/db/tests/test_connect_helpers.py`: the runner login comes from
`base.cluster.authority`, which sits above `base.db`, so the test lives with the cluster tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from base import db
from base.cluster.authority.tests.runner_logins import runner_projection
from base.config import settings
from base.db import code_version_gate as gate

_VERSION = 500


@pytest.fixture
def stored_minimum(db_conn: None) -> Iterator[int]:
    """The suite database's current `min_code_version`, restored when the test ends."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        row = conn.execute("SELECT min_code_version FROM deployment_state WHERE id = 1").fetchone()
        assert row is not None
        original = int(row[0])
        try:
            yield original
        finally:
            conn.execute("UPDATE deployment_state SET min_code_version = %s", (original,))


def test_a_runner_login_reads_the_minimum_and_cannot_write_it(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int
) -> None:
    """The gate needs no new grant: the runner group's blanket `SELECT` covers the
    column, and its inability to `UPDATE` the row (only the gateway raises the
    minimum) is unchanged."""
    process_gate = gate.ProcessDbGate(version=lambda: _VERSION, process="db-test")
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as admin:
        admin.execute("UPDATE deployment_state SET min_code_version = %s", (_VERSION,))
    runner_url = runner_projection()
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)

    with db.connect(gate=process_gate) as conn:
        assert process_gate.min_read_due() is False  # this dial read the minimum
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE deployment_state SET min_code_version = 0")
