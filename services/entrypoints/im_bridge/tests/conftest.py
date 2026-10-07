"""Core router doubles implement the durable adapter seam; stores stay real."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from services.entrypoints.im_bridge.outbound_types import (
    PreparedTimelineSend,
    TimelineAdapterKind,
    TimelineChunk,
)
from services.entrypoints.im_bridge.types import IMAdapter


@pytest.fixture(autouse=True)
def _core_durable_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    from services.entrypoints.im_bridge.tests import test_im_bridge_core

    monkeypatch.setenv("AVA_HOME", str(tmp_path))

    async def account(self: IMAdapter) -> str:
        return "test-account"

    async def prepare(self: IMAdapter, text: str) -> PreparedTimelineSend:
        return PreparedTimelineSend(
            adapter_kind=TimelineAdapterKind(self.channel + "-v1"),
            account_id="test-account",
            chunks=(TimelineChunk(text=text),),
            markdown=True,
        )

    async def send_prepared(self: IMAdapter, chat_id: str, prepared: PreparedTimelineSend) -> None:
        for chunk in prepared.chunks:
            await self.send(chat_id, chunk.text, markdown=prepared.markdown)

    monkeypatch.setattr(IMAdapter, "timeline_account_id", account)
    monkeypatch.setattr(IMAdapter, "prepare_timeline", prepare)
    monkeypatch.setattr(IMAdapter, "send_prepared_timeline", send_prepared)
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        monkeypatch.setattr(test_im_bridge_core, "TEST_POOL", pool)
        yield
