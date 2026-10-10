"""The memory-search composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from base.config import ConfigBoot
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_search import daemon


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


@pytest.mark.usefixtures("owned_config_environment")
def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    boot = ConfigBoot()
    monkeypatch.setattr(boot.view.services, "memory_search_port", 18765)
    monkeypatch.setattr(boot.view.services, "memory_search_max_batch_rows", 11)
    config = daemon.memory_search_config(config=boot)
    for field in dataclasses.fields(config):
        flat: Any = getattr(boot.view.services, field.name)
        assert getattr(config, field.name) == flat, field.name
    assert (config.memory_search_port, config.memory_search_max_batch_rows) == (18765, 11)


@pytest.mark.usefixtures("owned_config_environment")
async def test_storage_boot_reads_metadata_without_constructing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot = ConfigBoot()
    descriptor = factory.get_descriptor(boot.view.services.embedding_backend)
    constructor = Mock(side_effect=AssertionError("storage boot must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    store = Mock()
    store_constructor = Mock(return_value=store)
    server = Mock()
    server.serve = AsyncMock()
    monkeypatch.setattr(daemon, "MemoryStore", store_constructor)
    server_constructor = Mock(return_value=server)
    monkeypatch.setattr(daemon.uvicorn, "Server", server_constructor)
    config = daemon.memory_search_config(config=boot)
    await daemon.run(config, embedding_name_reader=lambda: boot.view.services.embedding_backend)
    store_constructor.assert_called_once_with(
        config.memory_search_data_dir / "vectors.npz",
        dim=descriptor.dim,
        fingerprint=descriptor.fingerprint,
    )
    store.load.assert_called_once_with()
    server.serve.assert_awaited_once_with()
    schemas = server_constructor.call_args.args[0].app.openapi()["components"]["schemas"]
    assert schemas["SearchBody"]["properties"]["vector"]["maxItems"] == descriptor.dim
    constructor.assert_not_called()


def test_schema_vector_bound_belongs_to_each_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.derived.memory_search.app import build_app
    from services.derived.memory_search.store import MemoryStore

    constructor = Mock(side_effect=AssertionError("schema boot must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    for dim in (3, 7):
        app = build_app(
            MemoryStore(tmp_path / f"vectors-{dim}.npz", dim=dim, fingerprint=f"test:{dim}"),
            10,
            embedding_dim=dim,
        )
        schemas = app.openapi()["components"]["schemas"]
        for name in ("UpsertBody", "SearchBody"):
            assert schemas[name]["properties"]["vector"]["maxItems"] == dim
    constructor.assert_not_called()
