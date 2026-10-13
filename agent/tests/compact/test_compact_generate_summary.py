"""Compact summary generation tests: `generate_summary` and the compact boundary stamp.

Split from `agent/tests/test_compact.py`, which holds the shared helpers and the auto-compact hook tests.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent.graph.hooks.compact import (
    COMPACTION_INSTRUCTION,
    generate_summary,
)
from agent.llm import execute_code
from agent.tests.test_compact import (
    compaction_ainvoke,
    make_fake_llm,
    pinned_slices,
    read_agent_field,
)
from base.config import settings
from base.lm import usage as usage_mod
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog


async def test_stamp_compact_boundary_writes_the_closing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.graph.hooks import compact
    from base.agents.history.closing_request import ClosingRequest

    seen: list[ClosingRequest | None] = []

    async def fake_mark(_pool: Any, _thread: str, *, closing: ClosingRequest | None = None) -> str:
        seen.append(closing)
        return "boundary"

    async def no_wait(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(compact, "mark_compact_boundary", fake_mark)
    monkeypatch.setattr(compact, "await_snapshot", no_wait)
    closing = ClosingRequest(100, 5, "m1")
    await compact.stamp_compact_boundary(
        MagicMock(), 1, None, closing=closing, read_agent=read_agent_field
    )
    await compact.stamp_compact_boundary(MagicMock(), 1, read_agent=read_agent_field)
    assert seen == [closing, None]


async def test_generate_summary_emits_agent_billing_span(
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A completed compaction call records its provider usage in the ledger.

    The regression this catches is removing billing emission from the compact
    path, which is the largest individual agent provider call.
    """
    from opentelemetry import trace as otel_trace

    from base.telemetry import tracing as tracing_mod

    class _Span:
        def __init__(self, name: str) -> None:
            self.name = name
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

        def end(self) -> None:
            pass

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[_Span] = []

        def start_span(self, _name: str, *, start_time: int | None = None) -> _Span:
            span = _Span(_name)
            self.spans.append(span)
            return span

    tracer = _Tracer()

    # The summary's billing behavior requires a known provider; a bare test
    # process has no installed provider plugins and must declare that input.
    def vendor_of_model(_model: str, *, catalog: ModelCatalog) -> str:
        assert catalog is model_catalog
        return "deepseek"

    monkeypatch.setattr(usage_mod, "vendor_of_model", vendor_of_model)
    monkeypatch.setattr(settings.observability, "trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "is_initialized", lambda: True)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    response = AIMessage(
        content="a complete summary",
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "input_token_details": {"cache_read": 800},
        },
    )
    llm = make_fake_llm(response=response)
    llm.model_name = "deepseek-flash"
    slices = pinned_slices({"llm_model": "deepseek-flash"})

    assert (
        await generate_summary(
            [HumanMessage(content="conversation")], llm, slices, catalog=model_catalog
        )
        == "a complete summary"
    )

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.attributes["ava.billing.model"] == "deepseek-flash"
    assert span.attributes["ava.billing.vendor"] == "deepseek"
    assert span.attributes["ava.billing.usage_kind"] == "agent"
    assert span.attributes["ava.billing.tokens_in"] == 1_000
    assert span.attributes["ava.billing.tokens_out"] == 100
    assert span.attributes["ava.billing.cache_read_tokens"] == 800
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "agent"


async def test_generate_summary_includes_whole_conversation():
    """Request = [*whole conversation, HumanMessage(COMPACTION_INSTRUCTION)] — no more hold-out
    tail, model sees every message (including ToolMessage), instruction enters as appended message."""
    ai = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}]
    )
    convo: list[AnyMessage] = [
        HumanMessage(content="q"),
        ai,
        ToolMessage(content="out", tool_call_id="c1"),
        AIMessage(content="done"),
    ]

    llm = make_fake_llm()
    await generate_summary(convo, llm, pinned_slices(), catalog=build_model_catalog())

    [call] = compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[:-1] == convo  # whole conversation, no hold-out
    assert isinstance(llm_input[-1], HumanMessage)
    assert llm_input[-1].content.startswith(COMPACTION_INSTRUCTION)


async def test_generate_summary_reuses_conversation_prefix_for_cache():
    """compaction request = main conversation + a final instruction — original SystemMessage kept in place (not replaced with compaction-specific system prompt), bind_tools same as main llm node (tools field participates in prefix rendering); the whole conversation is the prefix of the previous main request, combined they hit the backend prefix cache."""
    sys_msg = SystemMessage(content="<real agent sys prompt>")
    content: list[AnyMessage] = [HumanMessage(content=f"m-{i}") for i in range(5)]

    llm = make_fake_llm()
    await generate_summary([sys_msg, *content], llm, pinned_slices(), catalog=build_model_catalog())

    llm.bind_tools.assert_called_once_with([execute_code])
    [call] = compaction_ainvoke(llm).call_args_list
    [llm_input] = call.args
    assert llm_input[0] is sys_msg  # original object in-place — same byte-for-byte prefix
    assert llm_input[:-1] == [sys_msg, *content]
    assert llm_input[-1].content.startswith(COMPACTION_INSTRUCTION)


async def test_generate_summary_raises_on_empty_llm_text():
    """LLM returns empty text (e.g., defying instruction only gives tool_call) → RuntimeError —
    empty summary cannot be used to replace history."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs, make_fake_llm(summary_text=""), pinned_slices(), catalog=build_model_catalog()
        )


async def test_generate_summary_extracts_text_from_block_content():
    """Production provider (thinking enabled) returns block content [thinking, text] —
    summary must be the text block content, not the whole list's repr."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    block_response = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "the real summary"},
        ]
    )

    llm = make_fake_llm(response=block_response)
    summary = await generate_summary(msgs, llm, pinned_slices(), catalog=build_model_catalog())
    assert summary == "the real summary"


async def test_generate_summary_raises_on_tool_use_only_block_content():
    """Defying instruction only returns tool_use block (no text block) → response.text is empty →
    RuntimeError — cannot let block list repr become summary."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    tool_only = AIMessage(
        content=[{"type": "tool_use", "id": "c1", "name": "execute_code", "input": {"code": "x"}}]
    )

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs,
            make_fake_llm(response=tool_only),
            pinned_slices(),
            catalog=build_model_catalog(),
        )


async def test_generate_summary_raises_on_empty_conversation():
    """Only SystemMessage (no conversation) → ValueError — nothing to summarize."""
    with pytest.raises(ValueError, match="empty"):
        await generate_summary(
            [SystemMessage(content="<sys>")],
            make_fake_llm(),
            pinned_slices(),
            catalog=build_model_catalog(),
        )
