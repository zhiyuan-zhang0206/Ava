from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langgraph.runtime import Runtime

from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog


def make_fake_ops_pool() -> AsyncMock:
    cur = AsyncMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    cur_cm = AsyncMock()
    cur_cm.__aenter__.return_value = cur
    cur_cm.__aexit__.return_value = None
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    transaction_cm = AsyncMock()
    transaction_cm.__aenter__.return_value = conn
    transaction_cm.__aexit__.return_value = None
    conn.transaction = MagicMock(return_value=transaction_cm)
    conn_cm = AsyncMock()
    conn_cm.__aenter__.return_value = conn
    conn_cm.__aexit__.return_value = None
    pool = AsyncMock()
    pool.connection = MagicMock(return_value=conn_cm)
    pool.fake_cursor = cur
    return pool


def placeholder_runtime(
    ops_pool: Any = None, *, database_gate: ProcessDbGate
) -> Runtime[MeivaContext]:
    ctx = MeivaContext(
        ops_pool=ops_pool or MagicMock(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)
