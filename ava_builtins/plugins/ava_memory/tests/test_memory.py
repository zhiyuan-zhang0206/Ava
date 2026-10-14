"""`ava.memory.PATH` + `PERSONAL_PATH` + `search` + `IndexerUnavailable` unit tests.

After PR-1 (`feat: route ava.memory.search through gateway`), the SDK calls the
local gateway over HTTP, **no longer** importing the embedder or a vector store directly.
Data-plane behaviour unit tests live in `gateway/tests/routers/test_memory_search.py`
(gateway-side primary embeds + searches the memory-search service directly; secondary forwards). This file
only tests the SDK ↔ gateway wire and PATH prefix conversion.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ava
from ava.sdk_surface import install
from ava_builtins.plugins.ava_memory import plugin as memory_plugin
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
    from base.paths import workspace_dir
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(17)
    assert workspace_dir(17) / "memory" == ava.memory.PERSONAL_PATH
    assert isinstance(ava.memory.PERSONAL_PATH, Path)
    assert "PERSONAL_PATH" in ava.memory.__all_for_ava__


def test_personal_path_needs_an_agent_identity() -> None:
    from tests.fixtures.pin_agent import pin_no_identity

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
    from tests.fixtures.pin_agent import pin_agent

    pin_agent(17)
    doc = getattr(ava.memory, name).__doc__
    for required in ("type: Memory", "ava_agent:", "ava_machine:", "timestamp:", "title:"):
        assert required in doc
    assert "description:" in doc and "tags: [type/<kind>]" in doc
    assert "ava.files.write" in doc


# ── search SDK layer (HTTP wire) ─────────────────────────────────────────


def test_search_forwards_query_and_k_to_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK passes query + k to `gateway_client.memory_search`, no other processing."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(
        query: str, k: int, *, purpose: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
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

    def _fake(
        query: str, k: int, *, purpose: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
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

    def _fake(
        query: str, k: int, *, purpose: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["timeout"] is None


def test_search_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-provided `timeout` reaches the gateway client unmodified."""
    from ava import gateway_client

    captured: dict[str, Any] = {}

    def _fake(
        query: str, k: int, *, purpose: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q", timeout=7.0)
    assert captured["timeout"] == 7.0


def test_search_rejects_non_number_timeout() -> None:
    """A non-numeric `timeout` fails loud at the SDK boundary — the public
    path's coerce, not the wire (the fixture wrapper bypasses `search_unwrapped`'s
    validation, so exercise `search_unwrapped` directly)."""
    from ava_builtins.plugins.ava_memory.sdk import search_unwrapped as sdk_search

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
            MemorySearchResult("notes/foo.md", "desc1", (), 0.9, "foo passage"),
            MemorySearchResult("bar.md", "", (), 0.4, "bar passage"),
        ],
    )
    results = ava.memory.search("any")
    assert [m.path for m in results] == [
        ava.memory.PATH / "notes" / "foo.md",
        ava.memory.PATH / "bar.md",
    ]
    assert all(m.path.is_absolute() for m in results)


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


def test_search_returns_named_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each match carries the absolute path, description, tags, score and chunk, in order."""
    from ava import gateway_client
    from ava.gateway_client import MemorySearchResult

    monkeypatch.setattr(
        gateway_client,
        "memory_search",
        lambda _q, _k, **_kw: [  # pyright: ignore[reportUnknownArgumentType]
            MemorySearchResult(
                "notes/project.md", "Project note", ("type/project", "x"), 0.82, "the passage"
            ),
            MemorySearchResult("bar.md", "", (), 0.3, "other"),
        ],
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


def test_search_empty_result_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """No matches returns empty list."""
    from ava import gateway_client

    monkeypatch.setattr(gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    assert ava.memory.search("nothing") == []


def test_search_default_k_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default k=5."""
    from ava import gateway_client

    captured: dict[str, int] = {}

    def _fake(
        query: str, k: int, *, purpose: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
        captured["k"] = k
        return []

    monkeypatch.setattr(gateway_client, "memory_search", _fake)
    ava.memory.search("q")
    assert captured["k"] == 5
