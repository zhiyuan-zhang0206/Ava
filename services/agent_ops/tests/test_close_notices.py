"""The ops daemon's closure-notice flush waits for its unit's hold to release (issue #2044).

Every managed start — `ava start` after `ava stop`, a release's or a PITR
activation's start — brings the ops daemon up inside the maintenance hold that
start releases only after readiness. The flush must neither borrow the pool
inside that stop window nor give up on it: it waits for admission to reopen,
then delivers each notice exactly once.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from ops import pty_close_notices as notices
from ops.tests.test_pty_close_notices import _agent, _inbounds, _record
from ops.tests.test_pty_close_notices import journal as journal
from ops.tests.test_pty_close_notices import pool as pool
from services.agent_ops import close_notices

_HOLDER = "release:close-notices"
_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


async def test_a_notice_recorded_before_a_managed_start_lands_once_after_its_hold_releases(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del journal
    owner = _agent(db_conn, "running")
    _record(agent_id=owner)
    pause_owner.begin_maintenance(_HOLDER, _AT)
    starting = MaintenanceHold(phase="starting")
    pause_owner.change_maintenance(_HOLDER, _AT, MaintenanceHold(), starting)
    monkeypatch.setattr(close_notices, "_ADMISSION_POLL_S", 0.05)

    # The ops daemon starts inside the hold, as every managed start runs it.
    flush = asyncio.create_task(close_notices.deliver(pool))
    await asyncio.sleep(0.5)
    assert not flush.done(), "the flush waits out the hold instead of giving up on it"
    assert _inbounds(db_conn, owner) == []
    assert len(list(notices.journal_dir().iterdir())) == 1

    pause_owner.change_maintenance(
        _HOLDER, _AT, starting, MaintenanceHold(phase="ready"), resumed=True
    )
    await asyncio.wait_for(flush, timeout=30)
    assert len(_inbounds(db_conn, owner)) == 1
    assert list(notices.journal_dir().iterdir()) == []

    await close_notices.deliver(pool)  # a later start's flush finds nothing left
    assert len(_inbounds(db_conn, owner)) == 1
