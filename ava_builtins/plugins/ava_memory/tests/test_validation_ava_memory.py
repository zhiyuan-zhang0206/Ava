"""Argument validation of the memory plugin's entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.pin_agent import pin_agent


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

        memory_plugin._search(("query",))  # pyright: ignore[reportArgumentType]
        assert seen["q"] == "query"

    def test_search_query_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import gateway_client
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        monkeypatch.setattr(gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="query must be a string"):
            memory_plugin._search(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_write_slug_unwraps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Slug tuple unwraps; the entry lands under the unwrapped name."""
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin
        from base.paths import workspace_dir

        root = workspace_dir(900001) / "memory"
        pin_agent(900001)
        monkeypatch.setattr(
            memory_plugin,
            "_entry_path",
            lambda _slug, _store, _agent_id: (root / f"{_slug}.md", False),  # pyright: ignore[reportUnknownArgumentType]
        )
        monkeypatch.setattr(memory_plugin, "_write_atomically", lambda _path, _content: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(memory_plugin, "_upsert_index", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

        entry = memory_plugin.write(("slug-one",), ("content",), store=("personal",))  # pyright: ignore[reportArgumentType]
        assert entry == (root / "slug-one.md").resolve()

    def test_write_multi_element_slug_type_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        monkeypatch.setattr(
            memory_plugin,
            "_entry_path",
            lambda _slug, _store, _agent_id: (tmp_path / "x.md", False),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="slug must be a string"):
            memory_plugin.write(("a", "b"), "c")  # pyright: ignore[reportArgumentType]
