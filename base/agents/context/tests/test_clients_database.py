"""Native client construction keeps the explicit database independent of process defaults."""

import pytest

from base.agents.context.clients import ClientSet
from base.db.code_version_gate import ProcessDbGate


def test_custom_database_dials_its_own_resource_with_no_process_default(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    from base.config import settings
    from base.db import Database

    database = Database.from_settings(gate=database_gate)
    clients = ClientSet(database=lambda: database)
    monkeypatch.setattr(settings.data_plane, "db_url", "")
    try:
        assert clients.sql.execute("SELECT 42").fetchone() == (42,)
    finally:
        clients.close()
