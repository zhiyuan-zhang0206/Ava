"""The stop window: the labeler's poll loop skips its pass while the unit is quiesced, so it
borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.daemon.health import Liveness
from base.db import Database
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from services.derived.labeler import daemon


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_borrows_no_connection(
    monkeypatch: pytest.MonkeyPatch,
    quiesced: bool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.01)
    pool = MagicMock()
    task = asyncio.create_task(
        daemon._dispatch_loop(
            cast("ConnectionPool", pool),
            Database.from_settings(),
            EventBus.from_settings(),
            Liveness(daemon._LIVENESS_TIMEOUT_S),
            daemon.labeler_config(),
            catalog=model_catalog,
            llm_override=config_authority.runtime.lm.llm_override,
            overrides=ModelOverrides.from_pins({}),
        )
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.mock_calls) is (not quiesced)
