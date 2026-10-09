"""The memory-search composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from base.config import get_field, settings
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_search import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.services, "memory_search_port", 18765)
    monkeypatch.setattr(settings.services, "memory_search_max_batch_rows", 11)
    config = daemon.memory_search_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert (config.memory_search_port, config.memory_search_max_batch_rows) == (18765, 11)


async def test_storage_boot_reads_metadata_without_constructing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = factory.get_descriptor()
    constructor = Mock(side_effect=AssertionError("storage boot must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    store = Mock()
    store_constructor = Mock(return_value=store)
    server = Mock()
    server.serve = AsyncMock()
    monkeypatch.setattr(daemon, "MemoryStore", store_constructor)
    monkeypatch.setattr(daemon.uvicorn, "Server", Mock(return_value=server))
    config = daemon.memory_search_config()
    await daemon.run(config)
    store_constructor.assert_called_once_with(
        config.memory_search_data_dir / "vectors.npz",
        dim=descriptor.dim,
        fingerprint=descriptor.fingerprint,
    )
    store.load.assert_called_once_with()
    server.serve.assert_awaited_once_with()
    constructor.assert_not_called()


def test_schema_vector_bound_uses_metadata_without_constructing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from services.derived.memory_search import app

    descriptor = factory.get_descriptor()
    constructor = Mock(side_effect=AssertionError("schema imports must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    importlib.reload(app)
    for body in (app.UpsertBody, app.SearchBody):
        assert body.model_json_schema()["properties"]["vector"]["maxItems"] == descriptor.dim
    constructor.assert_not_called()
