"""Maintenance re-drive wakes remain outside ordinary backlog pacing."""

from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.lifecycle.test_hosted_backlog_recovery import (
    isolated_clocks as isolated_clocks,
)
from services.agent_runner.agent_host.tests.test_agent_host import _PendingScanPool
from services.agent_runner.agent_host.tests.test_turn_dispatcher import _ScanScheduler


class TestHostedHostWakePacing:
    async def test_held_cohort_wakes_are_never_paced(
        self, monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
    ) -> None:
        pool = _PendingScanPool([(17, False, False)])
        host = AgentHost(
            pool=cast(AsyncConnectionPool[Any], pool),
            checkpointer=object(),  # pyright: ignore[reportArgumentType]
            graph=object(),  # pyright: ignore[reportArgumentType]
            machine="this-box",
            bus=EventBus.from_settings(),
            db=Database.from_settings(),
            catalog=model_catalog,
        )

        def held_wakes(_fences: object) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(17, False),
                dispatcher.PendingInboundWake(23, False),
            ]

        monkeypatch.setattr(
            "services.agent_runner.agent_host.host.maintenance_receipts.pending_wakes", held_wakes
        )
        # The drain's held re-drive is update machinery: never paced.
        assert [
            (wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(30)
        ] == [(17, False), (23, False)]
        scheduler = _ScanScheduler()
        wake_dispatcher = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=host.pending_inbound_wakes,
            stale_after_s=30,
            recovery_wake_batch=1,
            recovery_wake_inflight=1,
        )
        await wake_dispatcher.scan_once()
        assert scheduler.woken == [17, 23]
