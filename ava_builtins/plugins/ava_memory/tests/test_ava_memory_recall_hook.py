"""The passive-recall hook's trigger gate (`plugins/ava_memory/agent_runtime.py`).

The hook fires on any inbound that is not a machine-originated wake-up: user
chat, a peer agent's message (`agent:`), a scheduled turn (`schedule:`), and
system notices all pass; `watcher:` and `shell:` completions are skipped. These
tests drive the real hook with the recall pass stubbed, so they cover the gate
(and the no-inbound no-op) — not the search/filter pipeline, which
`agent/graph/tests/prompt/test_memory_recall.py` owns.
"""

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent.graph.recall.memory_recall import PassiveRecall
from agent.messages import NoteTag, inbound_message, system_note_message
from agent.state import CompactState, MemoryState, build_agent_state
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY


def _default_reader(domain: str, field: str) -> Any:
    return getattr(getattr(settings, domain), field)


def _compact_disabled(_state: Any, _agent: AgentSlices, *, catalog: ModelCatalog) -> bool:
    assert catalog.models
    return False


@pytest.fixture
def _loaded() -> Any:
    """The ava_memory agent-runtime face, so the hook instance under test is the
    one `contribute()` declares (same fixture shape as test_ava_memory_notes.py)."""
    from ava_builtins.plugins.ava_memory import agent_runtime as _plugin

    return _plugin


@pytest.fixture
def _hook_env(_loaded: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Feature on, auto-compact off, recall stubbed to a successful pass."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", _compact_disabled)  # pyright: ignore[reportUnknownArgumentType]
    recall = AsyncMock(
        return_value=PassiveRecall(
            note=system_note_message(content="recalled", tag=NoteTag.MEMORY),
            paths={"a.md"},
        )
    )
    monkeypatch.setattr(_loaded, "passive_memory_recall", recall)
    return _loaded, recall


def _state(messages: list[AnyMessage], **fields: Any):
    return build_agent_state(EMPTY)(messages=messages, **fields)


def _runtime(catalog: ModelCatalog, database_gate: ProcessDbGate) -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=MagicMock(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=_default_reader),
        catalog=catalog,
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return RunnableConfig()


def _inbound(source: str) -> AnyMessage:
    return inbound_message(content="hello", source=source, inbound_id=1, body_start=0)


@pytest.mark.parametrize(
    "source",
    ["user", "agent:402", "schedule:7", "system"],
    ids=["user", "agent", "schedule", "system"],
)
async def test_fires_on_real_inbound_sources(
    _hook_env: Any, source: str, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """User chat, peer-agent messages, scheduled turns, and system notices all
    carry conversation the recall should search over."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    recall.assert_awaited_once()
    assert result is not None
    assert "a.md" in result["memory"].injected_paths


@pytest.mark.parametrize(
    "source",
    ["watcher:3", "shell:9"],
    ids=["watcher", "shell"],
)
async def test_skips_machine_wakeups(
    _hook_env: Any, source: str, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """Watcher and shell completions are machine-originated wake-ups whose
    payload is the notice itself — recalling durable notes over them is noise."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    recall.assert_not_awaited()
    assert result is None


async def test_fires_when_a_real_inbound_sits_among_wakeups(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """A mixed tail with one real inbound still fires — the gate rejects the
    turn only when *every* inbound is a wake-up."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            _inbound("watcher:3"),
            _inbound("agent:402"),
        ]
    )

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    recall.assert_awaited_once()
    assert result is not None


async def test_gateway_error_leaves_the_turn_running(
    _loaded: Any,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """The whole hook path — not just the recall function — survives a memory
    search that errors out.

    This is the shape that killed agent 405 on 2026-08-07: the hook runs inside
    `before_llm`, so an exception from the search unwinds the graph and ends the
    agent process. The real `passive_memory_recall` runs here (the other tests
    stub it), with the gateway returning an HTTP 500, so the assertion is that the
    turn continues with no recall to add.
    """
    from base.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", _compact_disabled)  # pyright: ignore[reportUnknownArgumentType]

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/memory/search"
        return httpx.Response(500, text="Server error")

    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])
    clients = ClientSet(
        factories={
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: 1,
                retry_delay_reader=lambda: 0.0,
                memory_deadline_reader=lambda: 15.0,
            )
        }
    )
    original = _runtime(model_catalog, database_gate=database_gate)
    runtime = Runtime(context=replace(original.context, clients=clients))
    try:
        with (
            httpx.Client(
                base_url="http://gateway.test", transport=httpx.MockTransport(respond)
            ) as client,
            clients.using_gateway(client),
        ):
            assert await hook(state, runtime, _config()) is None
        assert len(requests) == 1
    finally:
        clients.close()


async def test_recall_deadline_exceeded_skips_recall_this_turn(
    _loaded: Any,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """A recall pass slower than `memory_recall_deadline_seconds` degrades to
    no recall instead of stalling the turn's first LLM call.

    The 2026-08-29 first-before_llm 13s shape: a fleet wake queued searches
    behind the gateway's search endpoint semaphore, the recall pass waited the
    full queue time, and the agent's first LLM call waited with it. The
    deadline releases the turn; the next inbound turn retries recall.
    """
    import asyncio

    from base.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(settings.agent, "memory_recall_deadline_seconds", 0.05)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", _compact_disabled)  # pyright: ignore[reportUnknownArgumentType]

    async def _slow_recall(_messages: Any, **_kwargs: Any) -> PassiveRecall:
        await asyncio.sleep(0.5)
        return PassiveRecall(
            note=system_note_message(content="late", tag=NoteTag.MEMORY),
            paths={"late.md"},
        )

    monkeypatch.setattr(_loaded, "passive_memory_recall", _slow_recall)

    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])

    assert (
        await hook(state, _runtime(model_catalog, database_gate=database_gate), _config()) is None
    )


async def test_no_inbound_tail_is_a_noop(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """A silent-idle continue (bare AIMessage tail) carries no inbound at all —
    skipped, and mutually exclusive with hooks that claim that shape."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="idle", id="a0")])

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    recall.assert_not_awaited()
    assert result is None


# ── the per-window record ──


def _recalled(*paths: str) -> PassiveRecall:
    return PassiveRecall(
        note=system_note_message(content="recalled", tag=NoteTag.MEMORY), paths=set(paths)
    )


async def test_the_record_is_kept_while_the_window_is_unchanged(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """Notes already in this window stay excluded, and the new ones are written under the same
    window."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={"seen.md"}, window=1),
        compact=CompactState(version=1),
    )

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    assert recall.await_args.kwargs["injected_paths"] == {"seen.md"}
    assert result["memory"] == MemoryState(injected_paths={"a.md"}, window=1)


async def test_a_compaction_summarizes_the_injected_notes_away_so_none_is_excluded(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """The record outgrew the gateway's `exclude` limit (800) over many windows. After a
    compaction the notes are no longer in the context, so the pass excludes none of them and
    the same notes are recallable again. A record written before windows existed is window 0."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={f"note-{i}.md" for i in range(2088)}),
        compact=CompactState(version=3),
    )

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    assert recall.await_args.kwargs["injected_paths"] == frozenset()
    assert result["memory"] == MemoryState(injected_paths={"a.md"}, window=3)


async def test_a_compaction_resets_the_record_even_when_nothing_is_recalled(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """The reset does not wait for a note to be recalled: the stale record is replaced on the
    first pass after the compaction."""
    _loaded, recall = _hook_env
    recall.return_value = None
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={"old.md"}, window=0),
        compact=CompactState(version=2),
    )

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    assert recall.await_args.kwargs["injected_paths"] == frozenset()
    assert result == {"memory": MemoryState(window=2)}


async def test_a_compaction_resets_the_record_on_a_turn_that_does_not_recall(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """A watcher wake-up never searches, but the record it would have excluded is already
    out of the window, so it is replaced all the same."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("watcher:3")],
        memory=MemoryState(injected_paths={"old.md"}, window=0),
        compact=CompactState(version=1),
    )

    result = await hook(state, _runtime(model_catalog, database_gate=database_gate), _config())

    recall.assert_not_awaited()
    assert result == {"memory": MemoryState(window=1)}


async def test_after_a_compaction_only_notes_injected_since_are_excluded(
    _hook_env: Any, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    """The channel end to end: a note injected before a compaction is not excluded after it,
    and the notes injected after it are, so the record never holds more than one window."""
    _loaded, recall = _hook_env
    asked: list[set[str]] = []
    fresh = iter(["a.md", "b.md", "c.md"])

    async def _recall(_messages: Any, *, injected_paths: Any, **_kwargs: Any) -> PassiveRecall:
        asked.append(set(injected_paths))
        return _recalled(next(fresh))

    recall.side_effect = _recall
    hook = _loaded.passive_memory_recall_before_llm
    runtime = _runtime(model_catalog, database_gate=database_gate)
    state_cls = build_agent_state(EMPTY)

    async def recall_pass(state: Any) -> dict[str, Any] | None:
        return await hook(state, runtime, _config())

    async def compaction(_state: Any) -> dict[str, Any]:
        return {"compact": CompactState(version=1)}

    graph: Any = StateGraph(state_cls)
    steps = [
        ("recall_one", recall_pass),
        ("compact", compaction),
        ("recall_two", recall_pass),
        ("recall_three", recall_pass),
    ]
    previous = START
    for name, node in steps:
        graph.add_node(name, node)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)

    final = state_cls.model_validate(
        await graph.compile().ainvoke(
            state_cls(messages=[AIMessage(content="prev", id="a0"), _inbound("user")])
        )
    )

    assert asked == [set(), set(), {"b.md"}]
    assert final.memory == MemoryState(injected_paths={"b.md", "c.md"}, window=1)
