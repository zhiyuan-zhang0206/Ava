"""Passive memory recall rendering (`agent/graph/_memory_recall.py`).

Recall presents the pool-relative path plus the note's frontmatter `description`
-- two of the three fields `ava.memory.search` returns. It deliberately omits
tags because the injected note is a pointer, not a tag list. The description is
empty when absent, never synthesized from title/body, and is not truncated.
"""

from pathlib import Path

import httpx
import pytest

import agent.graph._memory_recall as recall
from agent.messages import inbound_message
from ava._gateway_client import MemorySearchResult
from shared.agents import IndexerUnavailable


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable the feature and point the on-disk sync check at a temp pool.

    The relevance filter is switched off for these: they are about what recall
    retrieves, dedups, and renders. Leaving it on would route every one of them
    through a model call — and pass only via its fallback, which is the opposite
    of testing the thing named in the test. The filter has its own file
    (`test_memory_filter.py`), and the two-stage composition is covered below.
    """
    monkeypatch.setattr("shared.config.settings.agent.passive_memory_recall_enabled", True)
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", False)
    monkeypatch.setattr(recall, "memory_dir", lambda: tmp_path)
    return tmp_path


def _write_note(root: Path, rel: str, text: str) -> None:
    note = root / rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(text, encoding="utf-8")


def _set_search(monkeypatch: pytest.MonkeyPatch, results: list[MemorySearchResult]) -> None:
    monkeypatch.setattr(recall._gateway_client, "memory_search", lambda _q, _k: results)  # pyright: ignore[reportUnknownArgumentType]


def _conversation() -> list:
    return [inbound_message(content="how do I deploy the gateway", source="user", inbound_id=1)]


async def test_renders_path_and_description(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each hit renders as `- <path>: <description>` from the search result."""
    _write_note(memory_root, "a.md", "note a")
    _write_note(memory_root, "sub/b.md", "note b")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="a.md", description="deploy runbook"),
            MemorySearchResult(path="sub/b.md", description="gateway ports"),
        ],
    )

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    assert "- a.md: deploy runbook" in result.note.content  # pyright: ignore[reportUnknownMemberType]
    assert "- sub/b.md: gateway ports" in result.note.content  # pyright: ignore[reportUnknownMemberType]
    assert result.paths == {"a.md", "sub/b.md"}


async def test_empty_description_renders_path_only_no_synthesis(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit with no frontmatter description renders bare `- <path>` -- recall
    does NOT fall back to the note's title or first body line (fail-fast: the
    injected note carries only what search returns)."""
    _write_note(
        memory_root,
        "c.md",
        "---\ntitle: A Human Title\n---\nfirst body line\n",
    )
    _set_search(monkeypatch, [MemorySearchResult(path="c.md", description="")])

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    content = result.note.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, str)
    assert "- c.md" in content.splitlines()
    assert "A Human Title" not in content
    assert "first body line" not in content
    assert result.paths == {"c.md"}


async def test_description_is_not_truncated(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The description is used verbatim (matching search, which does not
    truncate) -- no per-line char cap."""
    long_desc = "x" * 300
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [MemorySearchResult(path="a.md", description=long_desc)])

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    assert f"- a.md: {long_desc}" in result.note.content  # pyright: ignore[reportUnknownMemberType]


async def test_skips_already_injected_paths(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path already surfaced this session is dropped; only fresh paths return."""
    _write_note(memory_root, "a.md", "note a")
    _write_note(memory_root, "b.md", "note b")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="a.md", description="desc a"),
            MemorySearchResult(path="b.md", description="desc b"),
        ],
    )

    result = await recall.passive_memory_recall(_conversation(), injected_paths={"a.md"})  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    assert result.paths == {"b.md"}
    assert "- a.md" not in result.note.content  # pyright: ignore[reportUnknownMemberType]


async def test_skips_path_not_synced_to_this_machine(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search can return a path this machine has not synced yet; the missing
    file is skipped rather than crashing on read."""
    _write_note(memory_root, "here.md", "present")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="here.md", description="local"),
            MemorySearchResult(path="not-yet.md", description="remote"),
        ],
    )

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    assert result.paths == {"here.md"}
    assert "not-yet.md" not in result.note.content  # pyright: ignore[reportUnknownMemberType]


async def test_returns_none_when_all_matches_already_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [MemorySearchResult(path="a.md", description="desc a")])

    result = await recall.passive_memory_recall(_conversation(), injected_paths={"a.md"})  # pyright: ignore[reportUnknownArgumentType]

    assert result is None


# ── search failure degrades, never propagates ──
# Recall runs in a before_llm hook, so an exception escaping this function does
# not just lose the recall — it unwinds the graph and ends the agent process.
# On 2026-08-07 an intermittent 500 from /api/memory/search did exactly that to
# agent 405: `_raise_from_response` re-raises a status whose body carries no
# wire `reason`, and nothing between there and the graph caught it.


def _raise_on_search(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _boom(_q: str, _k: int) -> list[MemorySearchResult]:
        raise exc

    monkeypatch.setattr(recall._gateway_client, "memory_search", _boom)


def _status_error(status: int) -> httpx.HTTPStatusError:
    """The exception `_gateway_client.memory_search` raises for a non-2xx whose
    body does not carry the wire contract's `reason` — FastAPI's bare 500."""
    request = httpx.Request("POST", "http://gateway.test/api/memory/search")
    return httpx.HTTPStatusError(
        f"Server error '{status}'",
        request=request,
        response=httpx.Response(status, request=request),
    )


async def test_http_error_degrades_to_no_recall_and_logs_error(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """A gateway 500 leaves the turn with no recall instead of killing it, and
    says so at error level — nobody designed that response, so it is a bug to
    surface, not a state to ride out quietly."""
    _write_note(memory_root, "a.md", "note a")
    _raise_on_search(monkeypatch, _status_error(500))

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is None
    errors = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
    assert len(errors) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert errors[0]["extra"]["event"] == "passive_recall"
    assert "500" in errors[0]["message"]
    assert "/api/memory/search" in errors[0]["message"]


async def test_modelled_outage_degrades_without_an_error_log(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """The gateway naming its own outage in the wire contract is a self-clearing
    state (a restart, a stalled embedder), so it degrades the same way but stays
    below error — otherwise every gateway bounce reads as a bug."""
    _raise_on_search(monkeypatch, IndexerUnavailable("milvus search failed"))

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is None
    assert [r for r in loguru_records if r["level"].name == "ERROR"] == []  # pyright: ignore[reportUnknownMemberType]


async def test_programming_error_in_search_still_propagates(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation covers infrastructure failure only. A bug in the call
    path is not a memory-index outage and must still fail fast, or this handler
    becomes the bare `except` the fail-fast principle bans."""
    _raise_on_search(monkeypatch, TypeError("memory_search() got an unexpected keyword"))

    with pytest.raises(TypeError):
        await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]


async def test_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.config.settings.agent.passive_memory_recall_enabled", False)

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is None


async def test_returns_none_when_eval_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eval isolation must stop the direct index call that passive recall bypasses."""
    monkeypatch.setattr("shared.config.settings.agent.eval_isolation", True)
    called = False

    def _fake(_q: str, _k: int) -> list[MemorySearchResult]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(recall._gateway_client, "memory_search", _fake)

    assert await recall.passive_memory_recall(_conversation()) is None  # pyright: ignore[reportUnknownArgumentType]
    assert called is False


async def test_returns_none_when_no_query(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty conversation yields no query, so recall no-ops before searching."""
    called = False

    def _fake(_q: str, _k: int) -> list[MemorySearchResult]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(recall._gateway_client, "memory_search", _fake)

    result = await recall.passive_memory_recall([])

    assert result is None
    assert called is False


# ── two-stage recall ──
# Retrieval goes wide so the filter has candidates to reject; injection stays
# narrow. The two numbers were one before the filter existed.


async def test_retrieval_default_is_top_100(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrieval goes wide by default — the relaxed filter lists rather than
    rejects, so it needs real candidates to judge; the default lives in
    `shared/config/agent.py` (memory_recall_retrieve_k = 100)."""
    from shared.config import settings

    assert settings.agent.memory_recall_retrieve_k == 100
    asked: dict[str, int] = {}

    def _search(_q: str, k: int) -> list[MemorySearchResult]:
        asked["k"] = k
        return []

    monkeypatch.setattr(recall._gateway_client, "memory_search", _search)

    await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert asked["k"] == 100


async def test_retrieval_is_wider_than_injection(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`retrieve_k` is what search is asked for — not `inject_k`. A filter with
    only as many candidates as it may inject can rank but never reject."""
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_retrieve_k", 10)
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_inject_k", 3)
    asked: dict[str, int] = {}

    def _search(_q: str, k: int) -> list[MemorySearchResult]:
        asked["k"] = k
        return []

    monkeypatch.setattr(recall._gateway_client, "memory_search", _search)

    await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert asked["k"] == 10


async def test_only_what_the_filter_kept_is_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", True)
    for rel in ("a.md", "b.md", "c.md"):
        _write_note(memory_root, rel, "body")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="a.md", description="one"),
            MemorySearchResult(path="b.md", description="two"),
            MemorySearchResult(path="c.md", description="three"),
        ],
    )

    async def _keep_b(_query: str, _candidates: list) -> list[str]:
        return ["b.md"]

    monkeypatch.setattr(recall, "filter_candidates", _keep_b)  # pyright: ignore[reportUnknownArgumentType]

    result = await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert result is not None
    assert result.paths == {"b.md"}
    assert "a.md" not in result.note.content  # pyright: ignore[reportUnknownMemberType]


async def test_nothing_is_injected_when_the_filter_keeps_nothing(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of having a filter: an unfiltered recall always had its top-k to
    show, however weakly they matched."""
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", True)
    _write_note(memory_root, "a.md", "body")
    _set_search(monkeypatch, [MemorySearchResult(path="a.md", description="one")])

    async def _keep_none(_query: str, _candidates: list) -> list[str]:
        return []

    monkeypatch.setattr(recall, "filter_candidates", _keep_none)  # pyright: ignore[reportUnknownArgumentType]

    assert await recall.passive_memory_recall(_conversation()) is None  # pyright: ignore[reportUnknownArgumentType]


async def test_the_filter_sees_the_type_tag_search_returned(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tags travel from the search result into the candidate — without them the
    filter cannot be stricter with a profile note than with a procedure."""
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", True)
    _write_note(memory_root, "a.md", "body")
    _set_search(
        monkeypatch,
        [MemorySearchResult(path="a.md", description="one", tags=("type/user", "tech-ops"))],
    )
    seen: dict[str, list] = {}

    async def _capture(_query: str, candidates: list) -> list[str]:
        seen["candidates"] = candidates
        return []

    monkeypatch.setattr(recall, "filter_candidates", _capture)  # pyright: ignore[reportUnknownArgumentType]

    await recall.passive_memory_recall(_conversation())  # pyright: ignore[reportUnknownArgumentType]

    assert seen["candidates"][0].tags == ["type/user", "tech-ops"]  # pyright: ignore[reportUnknownMemberType]


async def test_already_injected_notes_still_reach_the_filter_then_dedup(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter judges the full candidate set even when some are already
    injected: dropping them first would leave a second, similar message with
    its best matches pre-removed, and the filter would inject unrelated notes
    that merely outranked the deduped ones. Dedup applies after the filter, to
    what it judged relevant."""
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", True)
    for rel in ("a.md", "b.md"):
        _write_note(memory_root, rel, "body")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="a.md", description="one"),
            MemorySearchResult(path="b.md", description="two"),
        ],
    )
    seen: dict[str, list] = {}

    async def _capture(_query: str, candidates: list) -> list[str]:
        seen["candidates"] = candidates
        # filter judges both notes relevant — the already-injected one included
        return [c.path for c in candidates]  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(recall, "filter_candidates", _capture)  # pyright: ignore[reportUnknownArgumentType]

    result = await recall.passive_memory_recall(_conversation(), injected_paths={"a.md"})  # pyright: ignore[reportUnknownArgumentType]

    # the filter saw both candidates (dedup did not run before it)
    assert [c.path for c in seen["candidates"]] == ["a.md", "b.md"]  # pyright: ignore[reportUnknownMemberType]
    # but only the fresh one is injected
    assert result is not None
    assert result.paths == {"b.md"}
    assert "a.md" not in result.note.content  # pyright: ignore[reportUnknownMemberType]


async def test_returns_none_when_everything_the_filter_kept_is_already_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter picks the same relevant note again on a similar second
    message; it is already in front of the agent, so nothing new is injected —
    and critically, no unrelated note is injected in its place."""
    monkeypatch.setattr("shared.config.settings.agent.memory_recall_filter_enabled", True)
    for rel in ("a.md", "b.md", "c.md"):
        _write_note(memory_root, rel, "body")
    _set_search(
        monkeypatch,
        [
            MemorySearchResult(path="a.md", description="one"),
            MemorySearchResult(path="b.md", description="two"),
            MemorySearchResult(path="c.md", description="three"),
        ],
    )

    async def _keep_a(_query: str, candidates: list) -> list[str]:
        return ["a.md"]

    monkeypatch.setattr(recall, "filter_candidates", _keep_a)  # pyright: ignore[reportUnknownArgumentType]

    result = await recall.passive_memory_recall(_conversation(), injected_paths={"a.md"})  # pyright: ignore[reportUnknownArgumentType]

    assert result is None
