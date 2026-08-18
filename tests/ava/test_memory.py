"""`ava.memory.PATH` + `search` + `IndexerUnavailable` unit tests.

After PR-1 (`feat: route ava.memory.search through gateway`), the SDK calls the
local gateway over HTTP, **no longer** importing pymilvus / embedder directly.
Data-plane behaviour unit tests live in `tests/gateway/test_memory_search.py`
(gateway-side primary embeds + milvus directly; secondary forwards). This file
only tests the SDK ↔ gateway wire and PATH prefix conversion.
"""

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

import ava
from shared.plugin_context import PluginContext

# ── Plugin simulation: wrap search() with the real implementation ───────
# In the agent process the ava_memory plugin wraps search() at startup.
# This fixture installs the same wrapper so tests work without the plugin.


@pytest.fixture(autouse=True)
def _wrap_memory_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the ava_memory plugin's search() wrapper so tests exercise the
    real search path instead of the RuntimeError stub."""

    # ava.memory is registered by the ava_memory native plugin (#830), not by
    # core. Under pytest-xdist a worker where a prior test cleared plugin
    # namespaces (clear_registered_namespaces) has no ava.memory, so the wrap
    # below errors at setup — (re)load the plugin to register the namespace.
    # sys.modules.pop forces module re-execution (register_namespace runs at
    # import) when it was imported before the clear.
    if not hasattr(ava, "memory"):
        sys.modules.pop("ava_builtins.plugins.ava_memory.plugin", None)
        with PluginContext("ava_memory"):
            importlib.import_module("ava_builtins.plugins.ava_memory.plugin")

    from ava import _gateway_client as _client

    def _wrapper(inner, query: str, k: int = 5):
        results = _client.memory_search(query, k)
        return [(ava.memory.PATH / r.path, r.description) for r in results]

    ava.extend.wrap("memory.search", _wrapper)


# ── PATH constant ────────────────────────────────────────────────────────


def test_path_is_absolute() -> None:
    """PATH is an absolute Path — agent uses `PATH / "xxx.md"` to build absolute paths."""
    assert ava.memory.PATH.is_absolute()


def test_path_under_unit_home() -> None:
    """PATH = `$AVA_HOME/memory/` — the memory pool lives under this unit's home."""
    assert ava.memory.PATH.name == "memory"
    assert (
        ava.memory.PATH.parent.name in (".ava", ".ava_gateway") or ava.memory.PATH.parent.exists()
    )


def test_path_is_path_object() -> None:
    """PATH is pathlib.Path, not str — agent can directly .iterdir() / .glob() / join /."""
    assert isinstance(ava.memory.PATH, Path)


# ── search SDK layer (HTTP wire) ─────────────────────────────────────────


def test_search_forwards_query_and_k_to_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK passes query + k to `_gateway_client.memory_search`, no other processing."""
    from ava import _gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int) -> list[dict[str, str]]:
        captured["query"] = query
        captured["k"] = k
        return []

    monkeypatch.setattr(_gateway_client, "memory_search", _fake)
    ava.memory.search("test query", k=7)
    assert captured == {"query": "test query", "k": 7}


def test_search_default_k_is_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """`k` default = 5 (consistent with gateway endpoint schema)."""
    from ava import _gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int) -> list[dict[str, str]]:
        captured["k"] = k
        return []

    monkeypatch.setattr(_gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["k"] == 5


def test_search_prefixes_paths_with_memory_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway returns relative paths; SDK prefixes `ava.memory.PATH` → absolute Path.

    fs-neutral design: regardless of whether the primary fs is /Users/x or /home/y,
    the gateway returns relative paths like `notes/foo.md`; the SDK joins them back
    to the local machine's memory pool on the caller side.
    """
    from ava import _gateway_client
    from ava._gateway_client import MemorySearchResult

    monkeypatch.setattr(
        _gateway_client,
        "memory_search",
        lambda _q, _k: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(path="notes/foo.md", description="desc1"),
            MemorySearchResult(path="bar.md", description=""),
        ],
    )
    results = ava.memory.search("any")
    assert results == [
        (ava.memory.PATH / "notes" / "foo.md", "desc1"),
        (ava.memory.PATH / "bar.md", ""),
    ]
    # all are tuple[Path, str]
    assert all(isinstance(p, Path) and p.is_absolute() and isinstance(d, str) for p, d in results)


def test_search_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway returns [] (no match) → SDK also returns []."""
    from ava import _gateway_client

    monkeypatch.setattr(_gateway_client, "memory_search", lambda _q, _k: [])  # pyright: ignore[reportUnknownArgumentType]
    assert ava.memory.search("nothing matches") == []


# ── IndexerUnavailable exception ─────────────────────────────────────────


def test_indexer_unavailable_is_exception() -> None:
    """IndexerUnavailable is an Exception subclass — agent can catch it."""
    assert issubclass(ava.memory.IndexerUnavailable, Exception)


def test_indexer_unavailable_is_wire_encoded() -> None:
    """After PR-1, IndexerUnavailable travels over the wire protocol (AvaAgentError
    subclass) — gateway side 503 + reason='indexer_unavailable', SDK reverse-looksup
    to reconstruct."""
    from shared.agents import AvaAgentError

    assert issubclass(ava.memory.IndexerUnavailable, AvaAgentError)


def test_indexer_unavailable_importable_but_not_in_all() -> None:
    """exception class is not in __all_for_ava__ (the rendered SDK surface only exposes
    the call surface), but remains reachable — agent still catches with
    `ava.memory.IndexerUnavailable`."""
    from shared.agents import IndexerUnavailable

    assert "IndexerUnavailable" not in ava.memory.__all_for_ava__
    assert ava.memory.IndexerUnavailable is IndexerUnavailable


def test_path_exported_in_all() -> None:
    assert "PATH" in ava.memory.__all_for_ava__


def test_search_exported_in_all() -> None:
    assert "search" in ava.memory.__all_for_ava__


# -- search returns (path, description) tuples --


def test_search_returns_path_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search returns (Path, description) tuples."""
    from ava import _gateway_client
    from ava._gateway_client import MemorySearchResult

    monkeypatch.setattr(
        _gateway_client,
        "memory_search",
        lambda _q, _k: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(path="notes/foo.md", description="My note about foo"),
            MemorySearchResult(path="bar.md", description=""),
        ],
    )
    results = ava.memory.search("any")
    assert results == [
        (ava.memory.PATH / "notes" / "foo.md", "My note about foo"),
        (ava.memory.PATH / "bar.md", ""),
    ]


def test_search_empty_result_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """No matches returns empty list."""
    from ava import _gateway_client

    monkeypatch.setattr(_gateway_client, "memory_search", lambda _q, _k: [])  # pyright: ignore[reportUnknownArgumentType]
    assert ava.memory.search("nothing") == []


def test_search_default_k_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default k=5."""
    from ava import _gateway_client

    captured: dict[str, int] = {}

    def _fake(query: str, k: int) -> list[dict[str, str]]:
        captured["k"] = k
        return []

    monkeypatch.setattr(_gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["k"] == 5
