from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent.messages import NoteTag, read_meiva_kwargs
from agent.state import build_agent_state
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.memory_search import MemorySearchResult
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.log.tests.log_records import LogRecords
from base.packages.plugins.extensions import ExtensionRegistry
from meiva.tests.fixtures.pin_agent import pin_agent

_SHARED = "meiva_memory__shared_notes"
_PERSONAL = "meiva_memory__personal_notes"

_GOOD = (
    "---\ntype: Memory\nmeiva_agent: 17\nmeiva_machine: box\ntimestamp: '2026-01-31T09:30:00+00:00'\n"
    "title: Retry rule\ndescription: Retries above three make the outage worse\n"
    "tags: [type/feedback]\n---\nThe service rate-limits above three retries.\n"
)


def _default_reader(domain: str, field: str) -> Any:
    return getattr(getattr(settings, domain), field)


@pytest.fixture
def hook() -> Any:
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    return agent_runtime.memory_write_check_after_exec


@pytest.fixture
def search() -> _MemoryGateway:
    return _MemoryGateway()


class _MemoryGateway:
    def __init__(self) -> None:
        self.results: list[MemorySearchResult] = []
        self.failure: str | None = None
        self.delay = 0.0
        self.calls: list[dict[str, Any]] = []
        self.finished = Event()
        self.finished.set()

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.finished.clear()
        try:
            self.calls.append(json.loads(request.content))
            assert request.method == "POST"
            assert request.url.path == "/api/memory/search"
            if self.delay:
                time.sleep(self.delay)
            if self.failure == "gateway":
                raise httpx.ConnectError("gateway restarting", request=request)
            if self.failure == "indexer":
                return httpx.Response(
                    503, json={"reason": "indexer_unavailable", "detail": "ranking model down"}
                )
            if self.failure == "http-500":
                return httpx.Response(500, text="boom")
            if self.failure == "malformed":
                return httpx.Response(200, json={"results": None})
            assert self.failure is None
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
        finally:
            self.finished.set()


def _hit(path: str, score: float, description: str = "") -> MemorySearchResult:
    return MemorySearchResult(path, description, ("type/feedback",), score, "passage")


def _state(
    shared: frozenset[str] | set[str] = frozenset(),
    personal: frozenset[str] | set[str] = frozenset(),
) -> Any:
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    registry = ExtensionRegistry((("meiva_memory", agent_runtime.contribute()),))
    recorded: dict[str, Any] = {_SHARED: set(shared), _PERSONAL: set(personal)}
    return build_agent_state(registry)(messages=[], **recorded)


@pytest.fixture
def runtime(search: _MemoryGateway, model_catalog: Any) -> Iterator[Runtime[MeivaContext]]:
    clients = ClientSet(
        gateway_url="http://gateway.test",
        gateway=lambda url: httpx.Client(
            base_url=url, transport=httpx.MockTransport(search.respond)
        ),
        factories={
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: 1,
                retry_delay_reader=lambda: 0.0,
                memory_deadline_reader=lambda: 15.0,
            )
        },
    )
    context = MeivaContext(
        identity=AgentIdentity(agent_id=17, owns_loop=True),
        agent=AgentSlices.resolve(default_reader=_default_reader),
        catalog=model_catalog,
        clients=clients,
    )
    try:
        yield Runtime(context=context)
    finally:
        assert search.finished.wait(timeout=2), "memory HTTP handler did not finish"
        clients.close()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def stores(unit_home: Path) -> tuple[Path, Path]:
    from base.paths import memory_dir, workspace_dir

    pin_agent(17)
    return memory_dir(), workspace_dir(17) / "memory"


def _reminders(result: dict[str, Any] | None) -> list[HumanMessage]:
    assert result is not None
    return result.get("messages", [])


def _is_cleared(result: dict[str, Any] | None) -> bool:
    assert result is not None
    return result[_SHARED] == Overwrite(set()) and result[_PERSONAL] == Overwrite(set())


async def test_nothing_recorded_calls_nothing(
    hook: Any, search: _MemoryGateway, runtime: Runtime[MeivaContext]
):
    assert await hook(_state(), runtime, RunnableConfig()) is None
    assert search.calls == []


async def test_a_clean_unique_note_adds_no_reminder_and_clears_the_record(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "topics/retry.md", _GOOD)
    search.results = [_hit("topics/other.md", 0.31), _hit("elsewhere.md", 0.05)]

    result = await hook(_state({"topics/retry.md"}), runtime, RunnableConfig())

    assert _is_cleared(result)
    assert _reminders(result) == []


async def test_the_search_asks_for_the_five_closest_notes(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "retry.md", _GOOD)

    await hook(_state({"retry.md"}), runtime, RunnableConfig())

    [call] = search.calls
    assert call["purpose"] == "dedup" and call["k"] == 5
    assert call["query"].startswith("Retries above three make the outage worse")
    assert "rate-limits above three retries" in call["query"]
    assert "meiva_agent" not in call["query"]
    assert call["deadline_seconds"] == settings.agent.memory_write_check_deadline_seconds


async def test_a_probable_duplicate_is_named_with_its_probability(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "new/retry.md", _GOOD)
    search.results = [
        _hit("old/retry-rule.md", 0.91, "Retry limits"),
        _hit("old/almost.md", 0.79),
    ]

    result = await hook(_state({"new/retry.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    assert read_meiva_kwargs(note).get("meiva_note_tag") == NoteTag.MEMORY_WRITE_CHECK.value
    text = str(note.content)
    assert str(pool / "new/retry.md") in text
    assert f"{pool / 'old/retry-rule.md'} (probability 0.91; it holds: Retry limits)" in text
    assert "almost.md" not in text
    assert "Nothing was changed or rejected" in text
    assert _is_cleared(result)


async def test_the_note_itself_is_never_its_own_duplicate(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "retry.md", _GOOD)
    search.results = [_hit("retry.md", 0.99), _hit("other.md", 0.4)]

    result = await hook(_state({"retry.md"}), runtime, RunnableConfig())

    assert _reminders(result) == []


async def test_only_the_five_closest_others_are_considered(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "retry.md", _GOOD)
    search.results = [_hit(f"low{i}.md", 0.1) for i in range(5)] + [_hit("sixth.md", 0.95)]

    result = await hook(_state({"retry.md"}), runtime, RunnableConfig())

    assert _reminders(result) == []


async def test_the_threshold_is_configurable(
    hook: Any,
    search: _MemoryGateway,
    stores: tuple[Path, Path],
    runtime: Runtime[MeivaContext],
):
    pool, _ = stores
    _write(pool, "retry.md", _GOOD)
    search.results = [_hit("other.md", 0.6)]

    assert _reminders(await hook(_state({"retry.md"}), runtime, RunnableConfig())) == []

    runtime = replace(
        runtime,
        context=replace(
            runtime.context,
            agent=AgentSlices.resolve(
                {"memory_write_dedup_threshold": 0.5}, default_reader=_default_reader
            ),
        ),
    )
    [note] = _reminders(await hook(_state({"retry.md"}), runtime, RunnableConfig()))
    assert "other.md" in str(note.content)


async def test_rule_violations_are_named_per_file(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    broken = _write(
        pool, "broken.md", "---\ntype: Memory\ndescription: d\ntags: [type/bogus]\n---\n"
    )

    result = await hook(_state({"broken.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    text = str(note.content)
    assert str(broken) in text
    assert "Missing required field 'meiva_agent'" in text
    assert "invalid type tag 'type/bogus'" in text


async def test_one_reminder_covers_every_note_of_the_call(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, own = stores
    _write(pool, "a.md", "no frontmatter")
    _write(pool, "b.md", _GOOD)
    _write(own, "mine.md", "also none")
    search.results = [_hit("old.md", 0.95)]

    result = await hook(_state({"a.md", "b.md"}, {"mine.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    text = str(note.content)
    assert (
        str(pool / "a.md") in text and str(pool / "b.md") in text and str(own / "mine.md") in text
    )
    assert (
        text.index(str(pool / "a.md"))
        < text.index(str(pool / "b.md"))
        < text.index(str(own / "mine.md"))
    )
    assert len(search.calls) == 2


async def test_personal_notes_get_the_rules_but_no_duplicate_search(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    _, own = stores
    _write(own, "clean.md", _GOOD)
    _write(own, "dirty.md", "no frontmatter")

    result = await hook(_state(set(), {"clean.md", "dirty.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    assert str(own / "dirty.md") in str(note.content) and "clean.md" not in str(note.content)
    assert search.calls == []


async def test_index_files_and_vanished_notes_are_skipped(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    _write(pool, "MEMORY.md", "no frontmatter, an index")
    _write(pool, "topics/index.md", "an index")

    result = await hook(
        _state({"MEMORY.md", "topics/index.md", "gone.md"}),
        runtime,
        RunnableConfig(),
    )

    assert _is_cleared(result) and _reminders(result) == []
    assert search.calls == []


@pytest.mark.parametrize("failure", ["indexer", "gateway", "http-500"])
async def test_an_unavailable_search_leaves_the_rules_findings_alone(
    hook: Any,
    search: _MemoryGateway,
    stores: tuple[Path, Path],
    runtime: Runtime[MeivaContext],
    failure: str,
):
    pool, _ = stores
    _write(pool, "broken.md", "no frontmatter")
    _write(pool, "fine.md", _GOOD)
    search.failure = failure

    result = await hook(_state({"broken.md", "fine.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    assert "broken.md" in str(note.content) and "fine.md" not in str(note.content)
    assert _is_cleared(result)
    assert search.calls
    assert all(call["purpose"] == "dedup" for call in search.calls)


async def test_a_deadline_overrun_is_logged_and_adds_nothing(
    hook: Any,
    search: _MemoryGateway,
    stores: tuple[Path, Path],
    runtime: Runtime[MeivaContext],
    loguru_records: LogRecords,
):
    pool, _ = stores
    _write(pool, "broken.md", "no frontmatter")
    runtime = replace(
        runtime,
        context=replace(
            runtime.context,
            agent=AgentSlices.resolve(
                {"memory_write_check_deadline_seconds": 0.05}, default_reader=_default_reader
            ),
        ),
    )
    search.delay = 0.4

    result = await hook(_state({"broken.md"}), runtime, RunnableConfig())

    assert _is_cleared(result) and _reminders(result) == []
    assert any("exceeded its 0.05s deadline" in line for line in loguru_records.messages("INFO"))


async def test_an_unexpected_error_is_logged_and_never_escapes(
    hook: Any,
    search: _MemoryGateway,
    stores: tuple[Path, Path],
    runtime: Runtime[MeivaContext],
    loguru_records: LogRecords,
):
    pool, _ = stores
    _write(pool, "retry.md", _GOOD)
    search.failure = "malformed"

    result = await hook(_state({"retry.md"}), runtime, RunnableConfig())

    assert _is_cleared(result) and _reminders(result) == []
    assert loguru_records.messages("ERROR") == ["[memory-write-check] check failed; no reminder"]
    assert len(search.calls) == 1


async def test_the_check_never_changes_a_note(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    pool, _ = stores
    messy = "---\ntype: Memory\n---\nbody\n"
    path = _write(pool, "messy.md", messy)
    search.results = [_hit("other.md", 0.99)]

    await hook(_state({"messy.md"}), runtime, RunnableConfig())

    assert path.read_text() == messy


async def test_records_union_across_calls_and_reset_in_a_real_graph(
    hook: Any, search: _MemoryGateway, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    from langgraph.graph import END, START, StateGraph

    pool, _ = stores
    _write(pool, "a.md", "no frontmatter")
    _write(pool, "b.md", "also none")
    state_cls: Any = type(_state())

    async def exec_one(_state: Any) -> dict[str, Any]:
        return {_SHARED: {"a.md"}}

    async def exec_two(_state: Any) -> dict[str, Any]:
        return {_SHARED: {"b.md"}}

    async def after_exec(state: Any) -> dict[str, Any] | None:
        return await hook(state, runtime, RunnableConfig())

    graph: Any = StateGraph(state_cls)
    graph.add_node("exec_one", exec_one)
    graph.add_node("exec_two", exec_two)
    graph.add_node("after_exec", after_exec)
    graph.add_edge(START, "exec_one")
    graph.add_edge("exec_one", "exec_two")
    graph.add_edge("exec_two", "after_exec")
    graph.add_edge("after_exec", END)

    final = state_cls.model_validate(await graph.compile().ainvoke(state_cls(messages=[])))

    assert getattr(final, _SHARED) == set() and getattr(final, _PERSONAL) == set()
    [reminder] = final.messages
    assert str(pool / "a.md") in str(reminder.content) and str(pool / "b.md") in str(
        reminder.content
    )


async def test_an_over_cap_note_is_named_with_its_character_count(
    hook: Any, stores: tuple[Path, Path], runtime: Runtime[MeivaContext]
):
    from meiva_builtins.plugins.meiva_memory.template import validate

    pool, _ = stores
    cap = validate.note_char_cap()
    _write(pool, "huge.md", _GOOD + "x" * (cap - len(_GOOD) + 1))

    result = await hook(_state({"huge.md"}), runtime, RunnableConfig())

    [note] = _reminders(result)
    text = str(note.content)
    assert str(pool / "huge.md") in text
    assert f"Note is {cap + 1} characters, over the {cap} cap" in text
