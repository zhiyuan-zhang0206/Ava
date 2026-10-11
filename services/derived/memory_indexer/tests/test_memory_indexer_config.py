"""The indexer root binds required provider and backend inputs to one boot owner."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.config import MemoryIndexerInputs
from services.derived.memory_indexer.tests.test_memory_indexer import _FakeProvider


def test_inputs_are_cold_and_owned() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first_inputs = MemoryIndexerInputs.from_boot(first)
    second_inputs = MemoryIndexerInputs.from_boot(second)
    assert not first.prepared and not second.prepared
    assert first.upgrades == second.upgrades == 0
    for owner, name, timeout, key in (
        (first, "first", 11.0, "first-key"),
        (second, "second", 22.0, "second-key"),
    ):
        owner.set_field("embedding_backend", name)
        owner.set_field("memory_embed_timeout_seconds", timeout)
        owner.set_field("gemini_api_key", SecretStr(key))
        owner.set_field("memory_search_backend", name)
        owner.set_field("memory_search_uri", f"http://{name}")
        owner.set_field("memory_indexer_reconcile_retry_backoff_seconds", timeout)
        owner.set_field("memory_indexer_reconcile_retry_backoff_cap_seconds", timeout * 2)
    assert first_inputs.embedding_name() == first_inputs.backend_name() == "first"
    assert second_inputs.embedding_name() == second_inputs.backend_name() == "second"
    assert first_inputs.search_uri() == "http://first"
    assert second_inputs.search_uri() == "http://second"
    assert (first_inputs.embed_timeout(), first_inputs.retry_base(), first_inputs.retry_cap()) == (
        11.0,
        11.0,
        22.0,
    )
    assert (
        second_inputs.embed_timeout(),
        second_inputs.retry_base(),
        second_inputs.retry_cap(),
    ) == (22.0, 22.0, 44.0)
    assert (first_inputs.api_key(), second_inputs.api_key()) == ("first-key", "second-key")


def test_inputs_observe_updates_without_crossing_owners() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first_inputs = MemoryIndexerInputs.from_boot(first)
    second_inputs = MemoryIndexerInputs.from_boot(second)
    for owner, key, timeout in ((first, "first-key", 11.0), (second, "second-key", 22.0)):
        owner.set_field("gemini_api_key", SecretStr(key))
        owner.set_field("memory_embed_timeout_seconds", timeout)
    assert (first_inputs.api_key(), second_inputs.api_key()) == ("first-key", "second-key")
    first.set_field("memory_embed_timeout_seconds", 33.0)
    first.set_field("gemini_api_key", None)
    assert first_inputs.embed_timeout() == 33.0
    assert first_inputs.api_key() is None
    assert second_inputs.embed_timeout() == 22.0
    assert second_inputs.api_key() == "second-key"


async def test_retry_constructs_selected_backend_with_required_uri_reader(
    monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
) -> None:
    requests: list[str] = []
    client_class = httpx.Client

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"count": 0})

    def client(*, base_url: str, timeout: float) -> httpx.Client:
        return client_class(
            base_url=base_url, timeout=timeout, transport=httpx.MockTransport(response)
        )

    monkeypatch.setattr(httpx, "Client", client)
    backend = await daemon._connect_backend_with_retry(
        Database.from_settings(gate=database_gate),
        _FakeProvider(),
        name="numpy",
        uri_reader=lambda: "http://selected-memory",
    )
    try:
        assert backend.name == "numpy"
        assert requests == ["http://selected-memory/meta"]
    finally:
        backend.close()
