"""`ava.memory.PATH` + `PERSONAL_PATH` + `search` + `IndexerUnavailable` unit tests.

After PR-1 (`feat: route ava.memory.search through gateway`), the SDK calls the
local gateway over HTTP, **no longer** importing the embedder or a vector store directly.
Data-plane behaviour unit tests live in `gateway/tests/routers/test_memory_search.py`
(gateway-side primary embeds + searches the memory-search service directly; secondary forwards). This file
only tests the SDK ↔ gateway wire and PATH prefix conversion.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest

import ava
from ava.gateway_client import use_client
from ava.sdk_surface import install
from ava_builtins.plugins.ava_memory import plugin as memory_plugin
from base.agents.context.gateway_client.memory_search import MemorySearchResult
from base.packages.plugins.extensions import ExtensionRegistry

# ── Plugin simulation: install the ava_memory SDK surface ───────────────


@pytest.fixture(autouse=True)
def _install_memory_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install the ava_memory SDK surface the plugin declares in the agent process."""
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    install.install(ExtensionRegistry((("ava_memory", memory_plugin.contribute()),)))
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


# ── PERSONAL_PATH constant ───────────────────────────────────────────────


def test_personal_path_is_the_memory_folder_of_the_agent_workspace() -> None:
    from ava.tests.fixtures.pin_agent import pin_agent
    from base.paths import workspace_dir

    pin_agent(17)
    assert workspace_dir(17) / "memory" == ava.memory.PERSONAL_PATH
    assert isinstance(ava.memory.PERSONAL_PATH, Path)
    assert "PERSONAL_PATH" in ava.memory.__all_for_ava__


def test_personal_path_needs_an_agent_identity() -> None:
    from ava.tests.fixtures.pin_agent import pin_no_identity

    pin_no_identity()
    with pytest.raises(AttributeError, match="needs an established agent identity"):
        _ = ava.memory.PERSONAL_PATH
    assert not hasattr(ava.memory, "PERSONAL_PATH")


def test_there_is_no_memory_writer() -> None:
    """Notes are written with `ava.files`; `ava.memory` has no `write`."""
    assert "write" not in ava.memory.__all_for_ava__
    assert not hasattr(ava.memory, "write")


@pytest.mark.parametrize("name", ["PATH", "PERSONAL_PATH"])
def test_path_docs_carry_the_note_format(name: str) -> None:
    """`help()` on either path shows the frontmatter fields and the one-type-tag rule."""
    from ava.tests.fixtures.pin_agent import pin_agent

    pin_agent(17)
    doc = getattr(ava.memory, name).__doc__
    for required in ("type: Memory", "ava_agent:", "ava_machine:", "timestamp:", "title:"):
        assert required in doc
    assert "description:" in doc and "tags: [type/<kind>]" in doc
    assert "ava.files.write" in doc


# ── search SDK layer (HTTP wire) ─────────────────────────────────────────


class _SearchWire(NamedTuple):
    requests: list[httpx.Request]
    results: list[MemorySearchResult]


@pytest.fixture
def search_wire() -> Iterator[_SearchWire]:
    wire = _SearchWire([], [])

    def respond(request: httpx.Request) -> httpx.Response:
        wire.requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/memory/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "path": hit.path,
                        "description": hit.description,
                        "tags": hit.tags,
                        "score": hit.score,
                        "chunk": hit.chunk,
                    }
                    for hit in wire.results
                ]
            },
        )

    with (
        httpx.Client(
            base_url="http://gateway.test", transport=httpx.MockTransport(respond)
        ) as client,
        use_client(client),
    ):
        yield wire


def test_search_forwards_query_and_k_to_gateway(search_wire: _SearchWire) -> None:
    """The SDK sends the query and result limit through the memory-search wire contract."""
    ava.memory.search("test query", k=7)
    [request] = search_wire.requests
    assert json.loads(request.content) == {"query": "test query", "k": 7, "purpose": "search"}


def test_search_default_k_is_5(search_wire: _SearchWire) -> None:
    """`k` default = 5 (consistent with gateway endpoint schema)."""
    ava.memory.search("q")
    [request] = search_wire.requests
    assert json.loads(request.content)["k"] == 5


def test_search_defaults_timeout_to_none(search_wire: _SearchWire) -> None:
    """No `timeout` leaves the gateway deadline unspecified on the wire."""
    ava.memory.search("q")
    [request] = search_wire.requests
    assert "deadline_seconds" not in json.loads(request.content)


def test_search_forwards_timeout(search_wire: _SearchWire) -> None:
    """A caller-provided `timeout` reaches the gateway unmodified, with an HTTP margin."""
    ava.memory.search("q", timeout=7.0)
    [request] = search_wire.requests
    assert json.loads(request.content)["deadline_seconds"] == 7.0
    assert request.extensions["timeout"]["read"] == 10.0


def test_search_rejects_non_number_timeout(search_wire: _SearchWire) -> None:
    """A non-numeric `timeout` fails at the SDK boundary before any HTTP request."""
    from ava_builtins.plugins.ava_memory.sdk import search_unwrapped as sdk_search

    with pytest.raises(TypeError, match="timeout must be"):
        sdk_search("q", timeout="fast")  # type: ignore[arg-type]
    assert search_wire.requests == []


def test_search_prefixes_paths_with_memory_path(search_wire: _SearchWire) -> None:
    """Gateway-relative paths become absolute paths in this machine's memory pool."""
    search_wire.results.extend(
        [
            MemorySearchResult("notes/foo.md", "desc1", (), 0.9, "foo passage"),
            MemorySearchResult("bar.md", "", (), 0.4, "bar passage"),
        ]
    )
    results = ava.memory.search("any")
    assert [m.path for m in results] == [
        ava.memory.PATH / "notes" / "foo.md",
        ava.memory.PATH / "bar.md",
    ]
    assert all(m.path.is_absolute() for m in results)


def test_search_empty_result(search_wire: _SearchWire) -> None:
    """Gateway returns [] (no match) → SDK also returns []."""
    assert ava.memory.search("nothing matches") == []


# ── IndexerUnavailable exception ─────────────────────────────────────────


def test_indexer_unavailable_is_exception() -> None:
    """IndexerUnavailable is an Exception subclass — agent can catch it."""
    assert issubclass(ava.memory.IndexerUnavailable, Exception)


def test_indexer_unavailable_is_wire_encoded() -> None:
    """After PR-1, IndexerUnavailable travels over the wire protocol (AvaAgentError
    subclass) — gateway side 503 + reason='indexer_unavailable', SDK reverse-looksup
    to reconstruct."""
    from base.api_contracts.agent_contract import AvaAgentError

    assert issubclass(ava.memory.IndexerUnavailable, AvaAgentError)


def test_indexer_unavailable_importable_but_not_in_all() -> None:
    """exception class is not in __all_for_ava__ (the rendered SDK surface only exposes
    the call surface), but remains reachable — agent still catches with
    `ava.memory.IndexerUnavailable`."""
    from base.api_contracts.agent_contract import IndexerUnavailable

    assert "IndexerUnavailable" not in ava.memory.__all_for_ava__
    assert ava.memory.IndexerUnavailable is IndexerUnavailable


def test_path_exported_in_all() -> None:
    assert "PATH" in ava.memory.__all_for_ava__


def test_search_exported_in_all() -> None:
    assert "search" in ava.memory.__all_for_ava__


def test_search_detailed_removed() -> None:
    assert "search_detailed" not in ava.memory.__all_for_ava__
    assert not hasattr(ava.memory, "search_detailed")


# -- search returns MemoryMatch tuples --


def test_search_returns_named_matches(search_wire: _SearchWire) -> None:
    """Each match carries the absolute path, description, tags, score and chunk, in order."""
    search_wire.results.extend(
        [
            MemorySearchResult(
                "notes/project.md", "Project note", ("type/project", "x"), 0.82, "the passage"
            ),
            MemorySearchResult("bar.md", "", (), 0.3, "other"),
        ]
    )
    results = ava.memory.search("project")
    assert results == [
        ava.memory.MemoryMatch(
            ava.memory.PATH / "notes" / "project.md",
            "Project note",
            ["type/project", "x"],
            0.82,
            "the passage",
        ),
        ava.memory.MemoryMatch(ava.memory.PATH / "bar.md", "", [], 0.3, "other"),
    ]
    first = results[0]
    assert (first.path, first.description, first.tags, first.score, first.chunk) == tuple(first)
    assert isinstance(first.tags, list)


def test_memory_match_is_exported_for_help() -> None:
    """The result type is on the agent surface so `help(ava.memory)` renders its fields."""
    assert "MemoryMatch" in ava.memory.__all_for_ava__
    assert ava.memory.MemoryMatch._fields == ("path", "description", "tags", "score", "chunk")


def test_search_empty_result_desc(search_wire: _SearchWire) -> None:
    """No matches returns empty list."""
    assert ava.memory.search("nothing") == []


def test_search_default_k_desc(search_wire: _SearchWire) -> None:
    """Default k=5."""
    ava.memory.search("q")
    [request] = search_wire.requests
    assert json.loads(request.content)["k"] == 5
