"""Provider inputs belong to each owner and remain live until an operation."""

from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pytest
from pydantic import SecretStr

from base.config import ConfigBoot
from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings.gemini import DIM, GeminiEmbeddingProvider
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    _AsyncClient,
    _patch_client,
)


async def test_two_owner_providers_read_only_at_operations_and_observe_updates(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
) -> None:
    fake = _AsyncClient(vectors=[[1.0] * DIM])
    _patch_client(monkeypatch, fake)
    with patch.dict(os.environ):
        first, second = ConfigBoot(), ConfigBoot()
        for owner, key, timeout in ((first, "first-key", 1.25), (second, "second-key", 2.5)):
            owner.set_field("gemini_api_key", SecretStr(key))
            owner.set_field("memory_embed_timeout_seconds", timeout)
        reads: list[tuple[str, str]] = []

        def provider(owner: ConfigBoot, name: str) -> GeminiEmbeddingProvider:
            def api_key() -> str | None:
                reads.append((name, "key"))
                value = owner.view.lm.gemini_api_key
                return None if value is None else value.get_secret_value()

            def timeout() -> float:
                reads.append((name, "timeout"))
                return owner.view.services.memory_embed_timeout_seconds

            return GeminiEmbeddingProvider(
                model_catalog, timeout_reader=timeout, api_key_reader=api_key
            )

        one, two = provider(first, "one"), provider(second, "two")
        assert reads == []
        np.testing.assert_equal(one.embed_batch([]).shape, (0, DIM))
        assert reads == [] and fake.call_count == 0
        await one.embed_query_async("first")
        await two.embed_query_async("second")
        first.set_field("gemini_api_key", SecretStr("updated-key"))
        first.set_field("memory_embed_timeout_seconds", 3.75)
        await one.embed_query_async("updated")
        assert [(call["headers"]["x-goog-api-key"], call["timeout"]) for call in fake.calls] == [
            ("first-key", 1.25),
            ("second-key", 2.5),
            ("updated-key", 3.75),
        ]
        assert reads == [
            (name, kind) for name in ("one", "two", "one") for kind in ("key", "timeout")
        ]
