"""Core router doubles implement the durable adapter seam; stores stay real."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from services.entrypoints.im_bridge.outbound.types import (
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
)
from services.entrypoints.im_bridge.types import IMAdapter
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.homes import unit_home as unit_home
from tests.fixtures.unit.sdk import _sdk_environment as _sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering


@pytest.fixture(autouse=True)
def _core_durable_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    from services.entrypoints.im_bridge.tests import test_im_bridge_core

    monkeypatch.setenv("AVA_HOME", str(tmp_path))

    async def account(self: IMAdapter) -> str:
        return "test-account"

    async def prepare(self: IMAdapter, text: str) -> PreparedOutboundSend:
        return PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind(self.channel + "-v1"),
            account_id="test-account",
            chunks=(OutboundChunk(text=text),),
            markdown=True,
        )

    async def send_prepared(self: IMAdapter, chat_id: str, prepared: PreparedOutboundSend) -> None:
        for chunk in prepared.chunks:
            await self.send(chat_id, chunk.text, markdown=prepared.markdown)

    monkeypatch.setattr(IMAdapter, "outbound_account_id", account)
    monkeypatch.setattr(IMAdapter, "prepare_timeline", prepare)
    monkeypatch.setattr(IMAdapter, "send_prepared_outbound", send_prepared)
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        monkeypatch.setattr(test_im_bridge_core, "TEST_POOL", pool)
        yield
