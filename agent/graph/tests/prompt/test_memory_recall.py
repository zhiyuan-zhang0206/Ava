"""Passive memory recall (`agent/graph/recall/memory_recall.py`).

Recall injects the notes the gateway's search scores at or above
`memory_recall_inject_threshold`, best first, at most `memory_recall_inject_max`, and presents
each as its pool-relative path, score, frontmatter description and matched passage. Tags are
left out: the injected note is a pointer, not a tag list. The description is empty when absent,
never synthesized from title/body, and is not truncated. Recall makes no model call of its own;
the scores come from the search result.
"""

import json
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from langchain_core.messages import AnyMessage

import agent.graph.recall.memory_recall as recall
from agent.messages import inbound_message
from base import config
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.memory_search import MemorySearchResult
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.config import settings
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable the feature and point the on-disk sync check at a temp pool."""
    monkeypatch.setattr(config.settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(recall, "memory_dir", lambda: tmp_path)
    return tmp_path


def _write_note(root: Path, rel: str, text: str) -> None:
    note = root / rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(text, encoding="utf-8")


def _result(
    path: str,
    *,
    score: float = 0.9,
    description: str = "",
    chunk: str = "",
    tags: tuple[str, ...] = (),
) -> MemorySearchResult:
    return MemorySearchResult(path, description, tags, score, chunk)


class _Asked(NamedTuple):
    """What one HTTP search request asked the gateway for."""

    k: int
    exclude: frozenset[str]
    timeout: float | None
    purpose: str
    query: str
    http_timeout: float | None


class _MemoryGateway:
    def __init__(self) -> None:
        self.results: list[MemorySearchResult] = []
        self.asked: list[_Asked] = []
        self.response: httpx.Response | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/memory/search"
        body = json.loads(request.content)
        self.asked.append(
            _Asked(
                body["k"],
                frozenset(body.get("exclude", ())),
                body.get("deadline_seconds"),
                body["purpose"],
                body["query"],
                request.extensions["timeout"]["read"],
            )
        )
        if self.response is not None:
            return self.response
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
                    for hit in self.results
                ]
            },
        )


@pytest.fixture
def search() -> _MemoryGateway:
    return _MemoryGateway()


def _set_search(search: _MemoryGateway, results: list[MemorySearchResult]) -> list[_Asked]:
    search.results = results
    return search.asked


@pytest.fixture
def context(search: _MemoryGateway) -> Iterator[AvaContext]:
    ctx = _context()
    try:
        with (
            httpx.Client(
                base_url="http://gateway.test", transport=httpx.MockTransport(search.respond)
            ) as client,
            ctx.clients.using_gateway(client),
        ):
            yield ctx
    finally:
        ctx.clients.close()


def _conversation() -> list[AnyMessage]:
    return [
        inbound_message(
            content="how do I deploy the gateway", source="user", inbound_id=1, body_start=0
        )
    ]


def _agent(**changes: Any) -> AgentSlices:
    return AgentSlices.resolve(default_reader=defaults(**changes))


def _context(agent_id: int | None = None) -> AvaContext:
    return AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
        clients=ClientSet(
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: 1,
                    retry_delay_reader=lambda: 0.0,
                    memory_deadline_reader=lambda: 15.0,
                )
            }
        ),
    )


async def _recall(
    context: AvaContext, injected: set[str] | None = None
) -> recall.PassiveRecall | None:
    return await recall.passive_memory_recall(
        _conversation(),
        context=context,
        agent=_agent(),
        injected_paths=injected or frozenset(),
    )


def _content(result: recall.PassiveRecall) -> str:
    content = result.note.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, str)
    return content


# ── rendering ──


async def test_renders_path_score_description_and_matched_passage(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    _write_note(memory_root, "a.md", "note a")
    _write_note(memory_root, "sub/b.md", "note b")
    _set_search(
        search,
        [
            _result("a.md", score=0.83, description="deploy runbook", chunk="restart the gateway"),
            _result(
                "sub/b.md",
                score=0.81,
                description="gateway ports",
                chunk="line one\n\nline three",
            ),
        ],
    )

    result = await _recall(context)

    assert result is not None
    content = _content(result)
    assert "- a.md (score 0.83): deploy runbook\n  > restart the gateway" in content
    assert "- sub/b.md (score 0.81): gateway ports\n  > line one\n  >\n  > line three" in content
    assert content.index("a.md") < content.index("sub/b.md")
    assert result.paths == {"a.md", "sub/b.md"}


async def test_tags_are_not_rendered(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    _write_note(memory_root, "a.md", "note a")
    _set_search(
        search, [_result("a.md", description="d", chunk="c", tags=("type/user", "tech-ops"))]
    )

    result = await _recall(context)

    assert result is not None
    assert "type/user" not in _content(result)
    assert "tech-ops" not in _content(result)


async def test_empty_description_is_not_synthesized(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """A hit with no frontmatter description renders no description -- recall does NOT fall back
    to the note's title or first body line (the injected note carries only what search returns)."""
    _write_note(memory_root, "c.md", "---\ntitle: A Human Title\n---\nfirst body line\n")
    _set_search(search, [_result("c.md", score=0.8, description="", chunk="")])

    result = await _recall(context)

    assert result is not None
    content = _content(result)
    assert "- c.md (score 0.80)" in content.splitlines()
    assert "A Human Title" not in content
    assert "first body line" not in content


async def test_passage_equal_to_the_description_is_not_repeated(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """A description-line hit matched the description itself; the passage would only repeat it."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(search, [_result("a.md", description="one-liner", chunk="one-liner")])

    result = await _recall(context)

    assert result is not None
    assert _content(result).count("one-liner") == 1


async def test_description_and_passage_are_not_truncated(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    long_desc, long_chunk = "x" * 300, "y" * 1800
    _write_note(memory_root, "a.md", "note a")
    _set_search(search, [_result("a.md", description=long_desc, chunk=long_chunk)])

    result = await _recall(context)

    assert result is not None
    assert f"{long_desc}\n  > {long_chunk}" in _content(result)


# ── threshold, cap, dedup ──


async def test_only_results_reaching_the_threshold_are_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    monkeypatch.setattr(config.settings.agent, "memory_recall_inject_threshold", 0.7)
    for name in ("hi", "edge", "lo"):
        _write_note(memory_root, f"{name}.md", name)
    _set_search(
        search,
        [_result("hi.md", score=0.95), _result("edge.md", score=0.7), _result("lo.md", score=0.69)],
    )

    result = await _recall(context)

    assert result is not None
    assert result.paths == {"hi.md", "edge.md"}


async def test_nothing_is_injected_when_no_result_reaches_the_threshold(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """Vector search always returns something; a turn where nothing scores high enough
    injects nothing rather than the best of a weak lot."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(search, [_result("a.md", score=0.3)])

    assert await _recall(context) is None


async def test_the_cap_keeps_the_best_scores_whatever_order_search_returned(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    monkeypatch.setattr(config.settings.agent, "memory_recall_inject_max", 2)
    scores = {"a": 0.75, "b": 0.99, "c": 0.8, "d": 0.9}
    for name in scores:
        _write_note(memory_root, f"{name}.md", name)
    _set_search(search, [_result(f"{n}.md", score=s) for n, s in scores.items()])

    result = await _recall(context)

    assert result is not None
    assert result.paths == {"b.md", "d.md"}
    content = _content(result)
    assert content.index("b.md") < content.index("d.md")


async def test_zero_cap_disables_injection_without_searching(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    monkeypatch.setattr(config.settings.agent, "memory_recall_inject_max", 0)
    asked = _set_search(search, [])

    assert await _recall(context) is None
    assert asked == []


async def test_skips_path_not_synced_to_this_machine(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    """Search can return a path this machine has not synced yet; the missing file is skipped
    rather than crashing on read, and does not use up a slot."""
    monkeypatch.setattr(config.settings.agent, "memory_recall_inject_max", 1)
    _write_note(memory_root, "here.md", "present")
    _set_search(search, [_result("not-yet.md", score=0.99), _result("here.md", score=0.8)])

    result = await _recall(context)

    assert result is not None
    assert result.paths == {"here.md"}
    assert "not-yet.md" not in _content(result)


# ── what search is asked for ──


@pytest.mark.parametrize("already_count", [0, 3, 40, 500])
async def test_search_asks_for_the_cap_whatever_the_session_has_injected(
    memory_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_count: int,
    context: AvaContext,
    search: _MemoryGateway,
) -> None:
    """Notes injected earlier ride along as an exclusion list; the number of results asked for
    stays the cap, so the ranking work of one pass does not grow with the session's age."""
    monkeypatch.setattr(config.settings.agent, "memory_recall_inject_max", 5)
    injected = {f"n{i}.md" for i in range(already_count)}
    asked = _set_search(search, [])

    await _recall(context, injected=injected)

    assert [(call.k, call.exclude) for call in asked] == [(5, frozenset(injected))]


async def test_search_is_bounded_by_the_recall_deadline(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    """The search request carries the pass's budget, so the gateway stops ranking when the
    pass has given up."""
    monkeypatch.setattr(config.settings.agent, "memory_recall_deadline_seconds", 2.5)
    asked = _set_search(search, [])

    await _recall(context)

    assert [call.timeout for call in asked] == [2.5]
    assert [call.http_timeout for call in asked] == [5.5]


async def test_search_is_issued_for_recall_not_as_an_agent_search(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """The gateway asks a different relevance question for a conversation than for a query."""
    await _recall(context)

    assert [call.purpose for call in search.asked] == ["recall"]
    assert "how do I deploy the gateway" in search.asked[0].query


def test_defaults_are_the_calibrated_values() -> None:
    """Calibrated by the 2026-10-11 benchmark for the default rerank model and prompts."""
    assert settings.agent.memory_recall_inject_threshold == 0.80
    assert settings.agent.memory_recall_inject_max == 3


# ── search failure degrades, never propagates ──
# Recall runs in a before_llm hook, so an exception escaping this function does
# not just lose the recall — it unwinds the graph and ends the agent process.
# On 2026-08-07 an intermittent 500 from /api/memory/search did exactly that to
# agent 405: `raise_from_response` re-raises a status whose body carries no
# wire `reason`, and nothing between there and the graph caught it.


async def test_http_error_degrades_to_no_recall_and_logs_error(
    memory_root: Path,
    loguru_records: list[dict],
    context: AvaContext,
    search: _MemoryGateway,
) -> None:
    """A gateway 500 leaves the turn with no recall instead of killing it, and
    says so at error level — nobody designed that response, so it is a bug to
    surface, not a state to ride out quietly."""
    _write_note(memory_root, "a.md", "note a")
    search.response = httpx.Response(500, text="Server error")

    assert await _recall(context) is None
    assert len(search.asked) == 1

    errors = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
    assert len(errors) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert errors[0]["extra"]["event"] == "passive_recall"
    assert "500" in errors[0]["message"]
    assert "/api/memory/search" in errors[0]["message"]


async def test_modelled_outage_degrades_without_an_error_log(
    memory_root: Path,
    loguru_records: list[dict],
    context: AvaContext,
    search: _MemoryGateway,
) -> None:
    """The gateway naming its own outage in the wire contract — including a failed ranking
    model call — is a self-clearing state, so it degrades the same way but stays below
    error. There is no fallback to unscored results."""
    _write_note(memory_root, "a.md", "note a")
    search.response = httpx.Response(
        503,
        json={
            "reason": "indexer_unavailable",
            "detail": "memory rerank failed (jev): upstream 503",
        },
    )

    assert await _recall(context) is None
    assert len(search.asked) == 1
    assert [r for r in loguru_records if r["level"].name == "ERROR"] == []  # pyright: ignore[reportUnknownMemberType]


async def test_programming_error_in_search_still_propagates(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """The degradation covers infrastructure failure only. A bug in the call
    path is not a memory-index outage and must still fail fast, or this handler
    becomes the bare `except` the fail-fast principle bans."""
    search.response = httpx.Response(200, json={"results": None})

    with pytest.raises(TypeError):
        await _recall(context)
    assert len(search.asked) == 1


# ── gating ──


async def test_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    monkeypatch.setattr(config.settings.agent, "passive_memory_recall_enabled", False)

    assert await _recall(context) is None
    assert search.asked == []


async def test_returns_none_when_eval_isolated(
    monkeypatch: pytest.MonkeyPatch, context: AvaContext, search: _MemoryGateway
) -> None:
    """Eval isolation must stop the direct index call that passive recall bypasses."""
    monkeypatch.setattr(config.settings.agent, "eval_isolation", True)
    asked = _set_search(search, [])

    assert await _recall(context) is None
    assert asked == []


async def test_returns_none_when_no_query(
    memory_root: Path, context: AvaContext, search: _MemoryGateway
) -> None:
    """An empty conversation yields no query, so recall no-ops before searching."""
    asked = _set_search(search, [])

    result = await recall.passive_memory_recall([], context=context, agent=_agent())

    assert result is None
    assert asked == []


async def test_recall_logs_search_time_and_injected_count(
    memory_root: Path,
    loguru_records: list[dict],
    context: AvaContext,
    search: _MemoryGateway,
) -> None:
    """The completed-pass event the `ava_memory` panels read carries `search_ms` and `injected`."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(search, [_result("a.md", score=0.9), _result("b.md", score=0.1)])

    await _recall(context)

    events = [r for r in loguru_records if r["extra"].get("event") == "passive_recall"]  # pyright: ignore[reportUnknownMemberType]
    assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert events[0]["extra"]["injected"] == 1
    assert isinstance(events[0]["extra"]["search_ms"], int)


async def test_gateway_search_receives_each_explicit_host_context(memory_root: Path) -> None:
    """Two host calls select their own HTTP clients without binding the shared SDK."""
    contexts = [_context(agent_id) for agent_id in (11, 22)]
    seen: list[int] = []
    with ExitStack() as stack:
        for agent_id, ctx in zip((11, 22), contexts, strict=True):
            stack.callback(ctx.clients.close)

            def respond(request: httpx.Request, *, selected: int = agent_id) -> httpx.Response:
                assert request.method == "POST"
                assert request.url.path == "/api/memory/search"
                seen.append(selected)
                return httpx.Response(200, json={"results": []})

            client = stack.enter_context(
                httpx.Client(
                    base_url=f"http://gateway-{agent_id}.test",
                    transport=httpx.MockTransport(respond),
                )
            )
            stack.enter_context(ctx.clients.using_gateway(client))
        for ctx in contexts:
            assert (
                await recall.passive_memory_recall(_conversation(), agent=_agent(), context=ctx)
                is None
            )
        assert seen == [11, 22]
