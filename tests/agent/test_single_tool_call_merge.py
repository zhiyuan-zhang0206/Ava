import asyncio
import json
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph import exec_node
from agent.graph._context import AvaContext
from agent.graph._tool_calls import merge_multiple_execute_code_tool_calls
from agent.state import AgentState
from ava_builtins.plugins.ava_syntax_fix.plugin import syntax_fix_before_exec
from tests.agent._fakes import make_fake_ops_pool


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "7"}}


def _ai_with_two_content_tool_uses() -> AIMessage:
    first_code = 'print("skills")'
    second_code = 'print("agents")'
    return AIMessage(
        id="ai_multi",
        content=[
            {"type": "thinking", "thinking": "plan", "index": 0},
            {
                "type": "tool_use",
                "id": "call_00",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": first_code}),
                "index": 1,
            },
            {
                "type": "tool_use",
                "id": "call_01",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": second_code}),
                "index": 2,
            },
        ],
        tool_calls=[
            {"name": "execute_code", "args": {"code": first_code}, "id": "call_00"},
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def test_merge_multiple_content_tool_uses_keeps_one_protocol_tool_call() -> None:
    merged = merge_multiple_execute_code_tool_calls(
        _ai_with_two_content_tool_uses(),
        agent_id=7,
        location="test",
    )

    assert merged is not None
    assert merged.id == "ai_multi"
    assert merged.response_metadata == {"stop_reason": "tool_use"}  # pyright: ignore[reportUnknownMemberType]
    assert merged.usage_metadata == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert len(merged.tool_calls) == 1
    assert merged.tool_calls[0]["id"] == "call_00"
    assert merged.tool_calls[0]["args"]["code"] == 'print("skills")\n\nprint("agents")'

    tool_use_blocks = [
        block
        for block in merged.content  # pyright: ignore[reportUnknownMemberType]
        if isinstance(block, dict) and block.get("type") == "tool_use"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert len(tool_use_blocks) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert tool_use_blocks[0]["id"] == "call_00"
    assert tool_use_blocks[0]["input"] == {"code": 'print("skills")\n\nprint("agents")'}


async def test_syntax_fix_before_exec_returns_merged_message() -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])

    update = await syntax_fix_before_exec(state, _runtime(), _config())

    assert update is not None
    fixed = update["messages"][0]
    assert len(fixed.tool_calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert fixed.tool_calls[0]["args"]["code"].rstrip("\n") == 'print("skills")\n\nprint("agents")'  # pyright: ignore[reportUnknownMemberType]


async def test_exec_node_merges_multiple_tool_uses_as_fallback(
    fake_cancel_event: asyncio.Event,
) -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])

    result = await exec_node(state, _runtime(), _config())

    assert result.update is not None
    messages = result.update["messages"]
    assert len(messages) == 2
    fixed_ai, tool_msg = messages
    assert len(fixed_ai.tool_calls) == 1
    assert tool_msg.tool_call_id == "call_00"
    assert "skills" in tool_msg.content
    assert "agents" in tool_msg.content


def _ai_with_hallucinated_tool_name() -> AIMessage:
    # Reproduces django__django-13417 from SWE-bench Verified: DeepSeek (via
    # the Anthropic-compatible endpoint) returned a tool_use block calling
    # `ava.files.edit` as if it were a registered tool. Anthropic Claude's
    # grammar-constrained decoding rejects this, but DeepSeek's compat layer
    # does not, so the wire reaches exec_node verbatim.
    return AIMessage(
        id="ai_hallucinated",
        content=[],
        tool_calls=[
            {
                "name": "ava.files.edit",
                "args": {"path": "/testbed/x.py", "old": "a", "new": "b"},
                "id": "call_99",
            },
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def test_exec_node_feeds_back_on_unknown_tool_name() -> None:
    state = AgentState(messages=[_ai_with_hallucinated_tool_name()])

    result = await exec_node(state, _runtime(), _config())

    assert result.update is not None
    assert result.goto == "after_exec"
    assert result.update["halted"] is False
    [tool_msg] = result.update["messages"]
    assert tool_msg.tool_call_id == "call_99"
    assert "ava.files.edit" in tool_msg.content
    assert "execute_code" in tool_msg.content


async def test_exec_node_feeds_back_when_code_key_missing() -> None:
    # execute_code name but malformed args ({} or missing "code" key) — same
    # KeyError path as the unknown-name case, must also feed back not crash.
    ai = AIMessage(
        id="ai_no_code",
        content=[],
        tool_calls=[{"name": "execute_code", "args": {"foo": "bar"}, "id": "call_88"}],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    state = AgentState(messages=[ai])

    result = await exec_node(state, _runtime(), _config())

    assert result.update is not None
    [tool_msg] = result.update["messages"]
    assert tool_msg.tool_call_id == "call_88"
    assert "execute_code" in tool_msg.content
