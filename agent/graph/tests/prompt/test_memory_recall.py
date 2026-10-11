"""Passive memory recall (`agent/graph/recall/memory_recall.py`).

Recall injects the notes the gateway's search scores at or above
`memory_recall_inject_threshold`, best first, at most `memory_recall_inject_max`, and presents
each as its pool-relative path, score, frontmatter description and matched passage. Tags are
left out: the injected note is a pointer, not a tag list. The description is empty when absent,
never synthesized from title/body, and is not truncated. Recall makes no model call of its own;
the scores come from the search result.
"""

from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AnyMessage

import agent.graph.recall.memory_recall as recall
from agent.messages import inbound_message
from ava.gateway_client import MemorySearchResult
from base.agents import IndexerUnavailable
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable the feature and point the on-disk sync check at a temp pool."""
    monkeypatch.setattr("base.config.settings.agent.passive_memory_recall_enabled", True)
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


def _set_search(monkeypatch: pytest.MonkeyPatch, results: list[MemorySearchResult]) -> list[int]:
    """Serve `results` from search; the returned list records each `k` search was asked for."""
    asked: list[int] = []

    def _search(_q: str, k: int, **_kwargs: object) -> list[MemorySearchResult]:
        asked.append(k)
        return results

    monkeypatch.setattr(recall.gateway_client, "memory_search", _search)
    return asked


def _conversation() -> list[AnyMessage]:
    return [
        inbound_message(
            content="how do I deploy the gateway", source="user", inbound_id=1, body_start=0
        )
    ]


def _agent() -> AgentSlices:
    return AgentSlices.resolve(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )


def _context() -> AvaContext:
    return AvaContext(catalog=build_model_catalog(), clock_factory=Clock.from_settings)  # pyright: ignore[reportUnknownArgumentType]


async def _recall(injected: set[str] | None = None) -> recall.PassiveRecall | None:
    return await recall.passive_memory_recall(
        _conversation(),
        context=_context(),
        agent=_agent(),
        injected_paths=injected or frozenset(),
    )


def _content(result: recall.PassiveRecall) -> str:
    content = result.note.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, str)
    return content


# ── rendering ──


async def test_renders_path_score_description_and_matched_passage(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_note(memory_root, "a.md", "note a")
    _write_note(memory_root, "sub/b.md", "note b")
    _set_search(
        monkeypatch,
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

    result = await _recall()

    assert result is not None
    content = _content(result)
    assert "- a.md (score 0.83): deploy runbook\n  > restart the gateway" in content
    assert "- sub/b.md (score 0.81): gateway ports\n  > line one\n  >\n  > line three" in content
    assert content.index("a.md") < content.index("sub/b.md")
    assert result.paths == {"a.md", "sub/b.md"}


async def test_tags_are_not_rendered(memory_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_note(memory_root, "a.md", "note a")
    _set_search(
        monkeypatch, [_result("a.md", description="d", chunk="c", tags=("type/user", "tech-ops"))]
    )

    result = await _recall()

    assert result is not None
    assert "type/user" not in _content(result)
    assert "tech-ops" not in _content(result)


async def test_empty_description_is_not_synthesized(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit with no frontmatter description renders no description -- recall does NOT fall back
    to the note's title or first body line (the injected note carries only what search returns)."""
    _write_note(memory_root, "c.md", "---\ntitle: A Human Title\n---\nfirst body line\n")
    _set_search(monkeypatch, [_result("c.md", score=0.8, description="", chunk="")])

    result = await _recall()

    assert result is not None
    content = _content(result)
    assert "- c.md (score 0.80)" in content.splitlines()
    assert "A Human Title" not in content
    assert "first body line" not in content


async def test_passage_equal_to_the_description_is_not_repeated(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A description-line hit matched the description itself; the passage would only repeat it."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [_result("a.md", description="one-liner", chunk="one-liner")])

    result = await _recall()

    assert result is not None
    assert _content(result).count("one-liner") == 1


async def test_description_and_passage_are_not_truncated(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_desc, long_chunk = "x" * 300, "y" * 1800
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [_result("a.md", description=long_desc, chunk=long_chunk)])

    result = await _recall()

    assert result is not None
    assert f"{long_desc}\n  > {long_chunk}" in _content(result)


# ── threshold, cap, dedup ──


async def test_only_results_reaching_the_threshold_are_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_threshold", 0.7)
    for name in ("hi", "edge", "lo"):
        _write_note(memory_root, f"{name}.md", name)
    _set_search(
        monkeypatch,
        [_result("hi.md", score=0.95), _result("edge.md", score=0.7), _result("lo.md", score=0.69)],
    )

    result = await _recall()

    assert result is not None
    assert result.paths == {"hi.md", "edge.md"}


async def test_nothing_is_injected_when_no_result_reaches_the_threshold(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vector search always returns something; a turn where nothing scores high enough
    injects nothing rather than the best of a weak lot."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [_result("a.md", score=0.3)])

    assert await _recall() is None


async def test_the_cap_keeps_the_best_scores_whatever_order_search_returned(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_max", 2)
    scores = {"a": 0.75, "b": 0.99, "c": 0.8, "d": 0.9}
    for name in scores:
        _write_note(memory_root, f"{name}.md", name)
    _set_search(monkeypatch, [_result(f"{n}.md", score=s) for n, s in scores.items()])

    result = await _recall()

    assert result is not None
    assert result.paths == {"b.md", "d.md"}
    content = _content(result)
    assert content.index("b.md") < content.index("d.md")


async def test_zero_cap_disables_injection_without_searching(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_max", 0)
    asked = _set_search(monkeypatch, [])

    assert await _recall() is None
    assert asked == []


async def test_already_injected_notes_are_set_aside_before_the_cap(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note surfaced earlier this session does not use up a slot: the cap fills with the
    next-best qualifying notes."""
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_max", 2)
    for name in ("a", "b", "c", "d"):
        _write_note(memory_root, f"{name}.md", name)
    _set_search(
        monkeypatch,
        [
            _result("a.md", score=0.99),
            _result("b.md", score=0.95),
            _result("c.md", score=0.9),
            _result("d.md", score=0.85),
        ],
    )

    result = await _recall(injected={"a.md", "b.md"})

    assert result is not None
    assert result.paths == {"c.md", "d.md"}
    assert "- a.md" not in _content(result)


async def test_returns_none_when_every_qualifying_note_is_already_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [_result("a.md")])

    assert await _recall(injected={"a.md"}) is None


async def test_skips_path_not_synced_to_this_machine(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search can return a path this machine has not synced yet; the missing file is skipped
    rather than crashing on read, and does not use up a slot."""
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_max", 1)
    _write_note(memory_root, "here.md", "present")
    _set_search(monkeypatch, [_result("not-yet.md", score=0.99), _result("here.md", score=0.8)])

    result = await _recall()

    assert result is not None
    assert result.paths == {"here.md"}
    assert "not-yet.md" not in _content(result)


# ── what search is asked for ──


async def test_search_asks_for_the_cap_plus_the_notes_already_injected(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already-injected notes still rank, so the request leaves room for them."""
    monkeypatch.setattr("base.config.settings.agent.memory_recall_inject_max", 5)
    asked = _set_search(monkeypatch, [])

    await _recall()
    await _recall(injected={"a.md", "b.md", "c.md"})

    assert asked == [5, 8]


async def test_search_is_issued_for_recall_not_as_an_agent_search(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway asks a different relevance question for a conversation than for a query."""
    purposes: list[str] = []

    def _search(_q: str, _k: int, *, purpose: str, **_kwargs: object) -> list[MemorySearchResult]:
        purposes.append(purpose)
        return []

    monkeypatch.setattr(recall.gateway_client, "memory_search", _search)

    await _recall()

    assert purposes == ["recall"]


async def test_search_request_never_exceeds_the_gateway_limit(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _set_search(monkeypatch, [])

    await _recall(injected={f"n{i}.md" for i in range(500)})

    assert asked == [100]


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


def _raise_on_search(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _boom(_q: str, _k: int, **_kwargs: object) -> list[MemorySearchResult]:
        raise exc

    monkeypatch.setattr(recall.gateway_client, "memory_search", _boom)


def _status_error(status: int) -> httpx.HTTPStatusError:
    """The exception `gateway_client.memory_search` raises for a non-2xx whose
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

    assert await _recall() is None

    errors = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
    assert len(errors) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert errors[0]["extra"]["event"] == "passive_recall"
    assert "500" in errors[0]["message"]
    assert "/api/memory/search" in errors[0]["message"]


async def test_modelled_outage_degrades_without_an_error_log(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """The gateway naming its own outage in the wire contract — including a failed ranking
    model call — is a self-clearing state, so it degrades the same way but stays below
    error. There is no fallback to unscored results."""
    _write_note(memory_root, "a.md", "note a")
    _raise_on_search(monkeypatch, IndexerUnavailable("memory rerank failed (jev): upstream 503"))

    assert await _recall() is None
    assert [r for r in loguru_records if r["level"].name == "ERROR"] == []  # pyright: ignore[reportUnknownMemberType]


async def test_programming_error_in_search_still_propagates(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation covers infrastructure failure only. A bug in the call
    path is not a memory-index outage and must still fail fast, or this handler
    becomes the bare `except` the fail-fast principle bans."""
    _raise_on_search(monkeypatch, TypeError("memory_search() got an unexpected keyword"))

    with pytest.raises(TypeError):
        await _recall()


# ── gating ──


async def test_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.config.settings.agent.passive_memory_recall_enabled", False)

    assert await _recall() is None


async def test_returns_none_when_eval_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eval isolation must stop the direct index call that passive recall bypasses."""
    monkeypatch.setattr("base.config.settings.agent.eval_isolation", True)
    asked = _set_search(monkeypatch, [])

    assert await _recall() is None
    assert asked == []


async def test_returns_none_when_no_query(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty conversation yields no query, so recall no-ops before searching."""
    asked = _set_search(monkeypatch, [])

    result = await recall.passive_memory_recall([], context=_context(), agent=_agent())

    assert result is None
    assert asked == []


async def test_recall_logs_search_time_and_injected_count(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """The completed-pass event the `ava_memory` panels read carries `search_ms` and `injected`."""
    _write_note(memory_root, "a.md", "note a")
    _set_search(monkeypatch, [_result("a.md", score=0.9), _result("b.md", score=0.1)])

    await _recall()

    events = [r for r in loguru_records if r["extra"].get("event") == "passive_recall"]  # pyright: ignore[reportUnknownMemberType]
    assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert events[0]["extra"]["injected"] == 1
    assert isinstance(events[0]["extra"]["search_ms"], int)


async def test_gateway_search_receives_each_explicit_host_context(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two host calls route their client dependencies without binding the shared SDK."""
    contexts = [
        AvaContext(
            identity=AgentIdentity(agent_id=11, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        ),
        AvaContext(
            identity=AgentIdentity(agent_id=22, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        ),
    ]
    seen: list[AvaContext] = []

    def search(
        _query: str, _k: int, *, purpose: str, context: AvaContext
    ) -> list[MemorySearchResult]:
        del purpose
        seen.append(context)
        return []

    monkeypatch.setattr(recall.gateway_client, "memory_search", search)
    for context in contexts:
        assert (
            await recall.passive_memory_recall(_conversation(), agent=_agent(), context=context)
            is None
        )
    assert seen == contexts
