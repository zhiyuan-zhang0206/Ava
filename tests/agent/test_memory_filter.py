"""The relevance filter between retrieval and injection.

Vector search always returns its top-k, so unfiltered recall injects notes on
every turn whether or not any of them fit. These tests are mostly about the
filter's ability to return *nothing*, and about it never taking a turn down when
the small model misbehaves.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from agent.graph._memory_filter import Candidate, filter_candidates


def _candidates(*paths: str) -> list[Candidate]:
    return [Candidate(path=p, description=f"about {p}", tags=["type/project"]) for p in paths]


def _model(reply: str | Exception) -> MagicMock:
    m = MagicMock()
    m.ainvoke = AsyncMock(
        side_effect=reply if isinstance(reply, Exception) else None,
        return_value=None if isinstance(reply, Exception) else AIMessage(content=reply),
    )
    return m


@pytest.fixture(autouse=True)
def _filter_on(monkeypatch: pytest.MonkeyPatch):
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "memory_recall_filter_enabled", True)
    monkeypatch.setattr(settings.agent, "memory_recall_inject_k", 3)


def _patch_model(monkeypatch: pytest.MonkeyPatch, reply: str | Exception) -> None:
    monkeypatch.setattr(
        "shared.lm.factory.build_chat_model",
        lambda _model_name, **_kw: _model(reply),  # pyright: ignore[reportUnknownArgumentType]
    )


async def test_retries_when_reply_unparseable_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky unparseable reply is retried (bounded), not reported as a
    warning: LLM output is statistically flaky, and a single bad reply is
    routine (user ruling 2026-08-05)."""

    replies = iter(["not a list at all", '["b.md"]'])
    m = MagicMock()
    m.ainvoke = AsyncMock(side_effect=lambda *_a, **_k: AIMessage(content=next(replies)))
    monkeypatch.setattr("shared.lm.factory.build_chat_model", lambda _model_name, **_kw: m)  # pyright: ignore[reportUnknownArgumentType]

    picked = await filter_candidates("q", _candidates("a.md", "b.md", "c.md"))

    assert picked == ["b.md"]
    assert m.ainvoke.await_count == 2  # one retry after the unparseable reply


async def test_warns_only_when_all_retries_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt failing is the only case worth a warning; it injects
    nothing rather than the unfiltered top-k."""
    import agent.graph._memory_filter as mf

    warned: list[str] = []
    monkeypatch.setattr(mf.logger, "warning", lambda *_a, **k: warned.append(str(k.get("body"))))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    m = MagicMock()
    m.ainvoke = AsyncMock(side_effect=lambda *_a, **_k: AIMessage(content="still not a list"))
    monkeypatch.setattr("shared.lm.factory.build_chat_model", lambda _model_name, **_kw: m)  # pyright: ignore[reportUnknownArgumentType]

    picked = await filter_candidates("q", _candidates("a.md", "b.md"))

    assert picked == []
    assert m.ainvoke.await_count == 3  # default max retries
    assert any("all 3 attempts failed" in w for w in warned), warned


async def test_keeps_only_what_the_model_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, '["b.md"]')

    picked = await filter_candidates("q", _candidates("a.md", "b.md", "c.md"))

    assert picked == ["b.md"]


async def test_filter_model_is_built_with_reasoning_pinned_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry defaults deepseek models to effort=max
    (shared/lm/registry.py), which made a filter call take ~80s against the 20s
    bound — every call timed out and recall silently injected the unfiltered
    top-3. The filter must pin reasoning off at the call site so a registry
    default change can never resurface that mode (review F1)."""
    from shared.lm._effort import ReasoningEffort

    seen: dict[str, object] = {}

    def _capture(_name: str, **_kw):
        seen.update(_kw)  # pyright: ignore[reportUnknownArgumentType]
        m = MagicMock()
        m.ainvoke = AsyncMock(return_value=AIMessage(content="[]"))
        return m

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _capture)  # pyright: ignore[reportUnknownArgumentType]

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == []
    assert seen["reasoning_effort"] == ReasoningEffort.NONE


async def test_injects_nothing_when_the_model_rejects_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter earning its place: unfiltered recall always had something to
    show, however weakly it matched."""
    _patch_model(monkeypatch, "[]")

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == []


async def test_model_order_is_kept_and_capped_at_inject_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "memory_recall_inject_k", 2)
    _patch_model(monkeypatch, '["c.md", "a.md", "b.md"]')

    assert await filter_candidates("q", _candidates("a.md", "b.md", "c.md")) == ["c.md", "a.md"]


@pytest.mark.parametrize(
    "reply",
    [
        'Sure! Here you go:\n```json\n["a.md"]\n```\nHope that helps.',
        '{"paths": ["a.md"]}',
    ],
    ids=["fenced-with-preamble", "nested-under-a-key"],
)
async def test_the_wrappers_small_models_add_are_seen_through(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """A fence, a sentence of preamble, or the array put under a key — the answer
    is still in there, and rejecting it would silently cost the precision the
    filter exists for."""
    _patch_model(monkeypatch, reply)

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == ["a.md"]


async def test_invented_path_is_dropped_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that was never a candidate cannot be injected. The rest of the
    model's answer is still usable, and taking the turn down over a helper
    model's slip would be the worse outcome."""
    _patch_model(monkeypatch, '["a.md", "hallucinated.md"]')

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == ["a.md"]


async def test_duplicate_picks_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, '["a.md", "a.md"]')

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == ["a.md"]


@pytest.mark.parametrize(
    "reply",
    ["not json at all", "", "[[nested]]"],
    ids=["prose", "empty", "not-strings"],
)
async def test_unreadable_reply_injects_nothing(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """A judge whose reply cannot be read must not smuggle in the unfiltered
    top-k it exists to reject: a broken filter degrades to *nothing*, not to
    the pre-filter behaviour."""
    _patch_model(monkeypatch, reply)

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == []


async def test_timeout_bound_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judging-call bound is AVA_MEMORY_RECALL_FILTER_TIMEOUT_SECONDS
    (task #698 G8): filter_candidates hands that value to wait_for, so the
    bound is config not a module literal."""
    import asyncio

    from shared.config import settings

    monkeypatch.setattr(settings.agent, "memory_recall_filter_timeout_seconds", 0.01)
    captured: dict[str, float] = {}
    real_wait_for = asyncio.wait_for

    def _wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        captured["timeout"] = timeout
        return real_wait_for(awaitable, timeout)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(asyncio, "wait_for", _wait_for)  # pyright: ignore[reportUnknownArgumentType]
    _patch_model(monkeypatch, '["a.md"]')

    picked = await filter_candidates("q", _candidates("a.md", "b.md"))

    assert picked == ["a.md"]
    assert captured["timeout"] == 0.01


async def test_model_failure_injects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recall runs on someone else's turn — a filter outage must not raise into
    it. But it must not silently degrade to the unfiltered top matches either:
    that is the failure mode the filter exists to remove (review F1 — the
    registry's max-effort default timed every call out and the old fallback hid
    it since launch)."""
    _patch_model(monkeypatch, RuntimeError("provider down"))

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == []


async def test_disabled_filter_passes_the_top_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "memory_recall_filter_enabled", False)

    def _boom(_name: str, **_kw):
        raise AssertionError("must not build a model when the filter is off")

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _boom)  # pyright: ignore[reportUnknownArgumentType]

    assert await filter_candidates("q", _candidates("a.md", "b.md")) == ["a.md", "b.md"]


async def test_no_candidates_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str, **_kw):
        raise AssertionError("must not build a model with nothing to judge")

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _boom)  # pyright: ignore[reportUnknownArgumentType]

    assert await filter_candidates("q", []) == []


async def test_prompt_lists_when_unsure_instead_of_staying_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt was relaxed from a strict judge to a generous lister: notes
    are cheap for the agent to ignore but expensive to miss, so the model is
    told to err on the side of including and let the agent decide. The old
    strict framing ("Be strict" / "[] is a good answer") must be gone, and the
    agent-reference rule that makes role/org notes surface must be there."""
    seen: dict[str, str] = {}

    def _capture(_name: str, **_kw):
        m = MagicMock()

        async def _ainvoke(messages, **_kw):
            seen["prompt"] = str(messages[0].content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            return AIMessage(content="[]")

        m.ainvoke = _ainvoke
        return m

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _capture)  # pyright: ignore[reportUnknownArgumentType]

    await filter_candidates("q", _candidates("a.md", "b.md"))

    assert "err on the side of including" in seen["prompt"]
    assert "When in doubt, list the note" in seen["prompt"]
    assert "agent id, a role, or a person" in seen["prompt"]
    assert "Be strict" not in seen["prompt"]
    assert "[] is a good answer" not in seen["prompt"]


async def test_prompt_shows_the_type_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tag is what the instruction's type/user|type/project rule keys on — a
    candidate rendered without it gives the model nothing to apply that rule
    to."""
    seen: dict[str, str] = {}

    def _capture(_name: str, **_kw):
        m = MagicMock()

        async def _ainvoke(messages, **_kw):
            seen["prompt"] = str(messages[0].content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            return AIMessage(content="[]")

        m.ainvoke = _ainvoke
        return m

    monkeypatch.setattr("shared.lm.factory.build_chat_model", _capture)  # pyright: ignore[reportUnknownArgumentType]

    await filter_candidates(
        "q", [Candidate(path="p.md", description="a profile", tags=["type/user", "tech-ops"])]
    )

    assert "type/user" in seen["prompt"]
    assert "a profile" in seen["prompt"]
    assert "type/user or type/project" in seen["prompt"]  # the strictness rule
