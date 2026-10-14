"""Argument validation of the memory plugin's entry points (`search`)."""

from __future__ import annotations

from typing import Any

import pytest


class TestMemoryEntries:
    def test_search_query_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import gateway_client
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "memory_search",
            lambda q, k, **_kw: seen.update(q=q, k=k) or [],  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        memory_plugin.search_unwrapped(("query",))  # pyright: ignore[reportArgumentType]
        assert seen["q"] == "query"

    def test_search_query_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import gateway_client
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        monkeypatch.setattr(gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="query must be a string"):
            memory_plugin.search_unwrapped(("a", "b"))  # pyright: ignore[reportArgumentType]
