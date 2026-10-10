"""`ava.memory.PATH` + `search` + `IndexerUnavailable` unit tests.

After PR-1 (`feat: route ava.memory.search through gateway`), the SDK calls the
local gateway over HTTP, **no longer** importing the embedder or a vector store directly.
Data-plane behaviour unit tests live in `gateway/routers/tests/test_memory_search.py`
(gateway-side primary embeds + searches the memory-search service directly; secondary forwards). This file
only tests the SDK ↔ gateway wire and PATH prefix conversion.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import ava
from ava.sdk_surface import install
from ava_builtins.plugins.ava_memory import plugin as memory_plugin
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkWrap

# ── Plugin simulation: wrap search() with the real implementation ───────
# In the agent process the ava_memory plugin wraps search() at startup.
# This fixture installs the same wrapper so tests work without the plugin.


@pytest.fixture(autouse=True)
def _wrap_memory_search(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install the ava_memory SDK surface with the search() wrapper the plugin
    declares in the agent process, so tests exercise the real search path instead
    of the RuntimeError stub."""
    from ava import gateway_client as _client
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)

    def _wrapper(
        inner: Callable[..., Any], query: str, k: int = 5, *, timeout: float | None = None
    ) -> list[tuple[Path, str, list[str]]]:
        results = _client.memory_search(query, k, timeout=timeout)
        return [(ava.memory.PATH / r.path, r.description, list(r.tags)) for r in results]

    declared = memory_plugin.contribute().merged(
        PluginContributions(sdk_wraps=(SdkWrap("memory.search", _wrapper),))
    )
    install.install(ExtensionRegistry((("ava_memory", declared),)))
    yield
    install.uninstall()


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
    """SDK passes query + k to `gateway_client.memory_search`, no other processing."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int, *, timeout: float | None = None) -> list[dict[str, str]]:
        captured["query"] = query
        captured["k"] = k
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("test query", k=7)
    assert captured == {"query": "test query", "k": 7}


def test_search_default_k_is_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """`k` default = 5 (consistent with gateway endpoint schema)."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int, *, timeout: float | None = None) -> list[dict[str, str]]:
        captured["k"] = k
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["k"] == 5


def test_search_defaults_timeout_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `timeout` passed → the client's dedicated default applies
    (gateway deadline + margin); the SDK does not invent one."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int, *, timeout: float | None = None) -> list[dict[str, str]]:
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["timeout"] is None


def test_search_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-provided `timeout` reaches the gateway client unmodified."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(query: str, k: int, *, timeout: float | None = None) -> list[dict[str, str]]:
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q", timeout=7.0)
    assert captured["timeout"] == 7.0


def test_search_rejects_non_number_timeout() -> None:
    """A non-numeric `timeout` fails loud at the SDK boundary — the public
    path's coerce, not the wire (the fixture wrapper bypasses `_search`'s
    validation, so exercise `_search` directly)."""
    from ava_builtins.plugins.ava_memory.sdk import _search as sdk_search

    with pytest.raises(TypeError, match="timeout must be"):
        sdk_search("q", timeout="fast")  # type: ignore[arg-type]


def test_search_prefixes_paths_with_memory_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway returns relative paths; SDK prefixes `ava.memory.PATH` → absolute Path.

    fs-neutral design: regardless of whether the primary fs is /Users/x or /home/y,
    the gateway returns relative paths like `notes/foo.md`; the SDK joins them back
    to the local machine's memory pool on the caller side.
    """
    from ava import gateway_client
    from ava.gateway_client import MemorySearchResult

    monkeypatch.setattr(
        gateway_client,
        "memory_search",
        lambda _q, _k, **_kw: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(path="notes/foo.md", description="desc1"),
            MemorySearchResult(path="bar.md", description=""),
        ],
    )
    results = ava.memory.search("any")
    assert results == [
        (ava.memory.PATH / "notes" / "foo.md", "desc1", []),
        (ava.memory.PATH / "bar.md", "", []),
    ]
    # all are tuple[Path, str, list[str]]
    assert all(
        isinstance(p, Path) and p.is_absolute() and isinstance(d, str) and isinstance(tags, list)
        for p, d, tags in results
    )


def test_search_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway returns [] (no match) → SDK also returns []."""
    from ava import gateway_client

    monkeypatch.setattr(gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    assert ava.memory.search("nothing matches") == []


# ── IndexerUnavailable exception ─────────────────────────────────────────


def test_indexer_unavailable_is_exception() -> None:
    """IndexerUnavailable is an Exception subclass — agent can catch it."""
    assert issubclass(ava.memory.IndexerUnavailable, Exception)


def test_indexer_unavailable_is_wire_encoded() -> None:
    """After PR-1, IndexerUnavailable travels over the wire protocol (AvaAgentError
    subclass) — gateway side 503 + reason='indexer_unavailable', SDK reverse-looksup
    to reconstruct."""
    from base.agents import AvaAgentError

    assert issubclass(ava.memory.IndexerUnavailable, AvaAgentError)


def test_indexer_unavailable_importable_but_not_in_all() -> None:
    """exception class is not in __all_for_ava__ (the rendered SDK surface only exposes
    the call surface), but remains reachable — agent still catches with
    `ava.memory.IndexerUnavailable`."""
    from base.agents import IndexerUnavailable

    assert "IndexerUnavailable" not in ava.memory.__all_for_ava__
    assert ava.memory.IndexerUnavailable is IndexerUnavailable


def test_path_exported_in_all() -> None:
    assert "PATH" in ava.memory.__all_for_ava__


def test_search_exported_in_all() -> None:
    assert "search" in ava.memory.__all_for_ava__


def test_search_detailed_removed() -> None:
    assert "search_detailed" not in ava.memory.__all_for_ava__
    assert not hasattr(ava.memory, "search_detailed")


# -- search returns (path, description, tags) tuples --


def test_search_returns_path_description_and_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search returns (Path, description, tags) tuples."""
    from ava import gateway_client
    from ava.gateway_client import MemorySearchResult

    monkeypatch.setattr(
        gateway_client,
        "memory_search",
        lambda _q, _k, **_kw: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(path="notes/foo.md", description="My note about foo"),
            MemorySearchResult(path="bar.md", description=""),
        ],
    )
    results = ava.memory.search("any")
    assert results == [
        (ava.memory.PATH / "notes" / "foo.md", "My note about foo", []),
        (ava.memory.PATH / "bar.md", "", []),
    ]


def test_search_returns_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tags flow through from the gateway result as a mutable SDK list."""
    from ava import gateway_client
    from ava.gateway_client import MemorySearchResult

    monkeypatch.setattr(
        gateway_client,
        "memory_search",
        lambda _q, _k, **_kw: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(
                path="notes/project.md",
                description="Project note",
                tags=("type/project", "x"),
            )
        ],
    )
    result = ava.memory.search("project")
    assert result[0][2] == ["type/project", "x"]


def test_search_empty_result_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """No matches returns empty list."""
    from ava import gateway_client

    monkeypatch.setattr(gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    assert ava.memory.search("nothing") == []


def test_search_default_k_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default k=5."""
    from ava import gateway_client

    captured: dict[str, int] = {}

    def _fake(query: str, k: int, *, timeout: float | None = None) -> list[dict[str, str]]:
        captured["k"] = k
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["k"] == 5
