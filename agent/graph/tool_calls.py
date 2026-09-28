"""Native tool-call normalization and per-call code replacement.

Keep every invocation and its ID. Structured provider content can contain calls
missing from LangChain's normalized list; recover those without joining code.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import AIMessage, ToolCall


def code_from_args(args: Any, *, source: str) -> str:
    """The `code` argument of a tool call, strictly. Raises if `args` is not a
    dict or `code` is present but not a string; returns "" only when `code` is
    absent. The single strict extractor the exec path and the llm-node
    log line share, so both fail loud on a malformed tool call the same way."""
    if not isinstance(args, dict):
        raise TypeError(f"{source}.args must be dict, got {type(args).__name__}")
    code = cast(dict[str, Any], args).get("code")
    if code is None:
        return ""
    if not isinstance(code, str):
        raise TypeError(f"{source}.args['code'] must be str, got {type(code).__name__}")
    return code


def first_tool_call_code(tool_calls: Sequence[ToolCall]) -> str:
    """The first tool call's `code` argument if it is a non-empty string, else ""
    — the graceful read for before_llm / before_exec hooks that bail when there
    is no code (the strict `code_from_args` is for the exec path that must run
    it). Mirrors the hand-rolled `tool_calls[0]["args"].get("code")` those hooks
    used to duplicate."""
    if not tool_calls:
        return ""
    code = tool_calls[0]["args"].get("code")
    return code if isinstance(code, str) else ""


def _call_from_content(block: dict[str, Any]) -> ToolCall:
    args = block.get("input")
    if not args and block.get("partial_json"):
        args = json.loads(block["partial_json"])
    if not isinstance(args, dict):
        raise TypeError(f"content tool_use {block['id']!r}.input must be dict")
    return ToolCall(
        name=block["name"], args=cast(dict[str, Any], args), id=block["id"], type="tool_call"
    )


def normalize_tool_calls(message: AIMessage) -> AIMessage | None:
    """Recover structured content calls in provider order, preserving each call.

    Return a same-ID replacement only when the native list needs correction.
    Content, arguments, metadata and unrelated invalid calls remain intact.
    """
    native = {call["id"]: call for call in message.tool_calls}
    calls: list[ToolCall] = []
    seen: set[str | None] = set()
    content: Any = message.content
    if isinstance(content, list):
        for block in cast(list[Any], content):
            if not isinstance(block, dict) or cast(dict[str, Any], block).get("type") != "tool_use":
                continue
            block = cast(dict[str, Any], block)
            call_id = block["id"]
            if call_id in seen:
                raise ValueError(f"Duplicate tool_call id: {call_id!r}")
            call = native[call_id] if call_id in native else _call_from_content(block)
            calls.append(call)
            seen.add(call_id)
    calls.extend(call for call in message.tool_calls if call["id"] not in seen)
    ids = [call["id"] for call in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate tool_call ids")
    if calls == message.tool_calls:
        return None
    return message.model_copy(update={"tool_calls": calls})


def replace_execute_code(message: AIMessage, tool_call_id: str | None, code: str) -> AIMessage:
    """Replace one call's code and matching content block, preserving siblings."""
    calls = list(message.tool_calls)
    index = next(i for i, call in enumerate(calls) if call["id"] == tool_call_id)
    call = calls[index]
    calls[index] = {**call, "args": {**call["args"], "code": code}}
    content: Any = message.content
    if isinstance(content, list):
        blocks: list[Any] = []
        for block in cast(list[Any], content):
            if (
                isinstance(block, dict)
                and cast(dict[str, Any], block).get("type") == "tool_use"
                and block["id"] == tool_call_id
            ):
                updated = {**cast(dict[str, Any], block), "input": calls[index]["args"]}
                if "partial_json" in updated:
                    updated["partial_json"] = json.dumps(calls[index]["args"], ensure_ascii=False)
                blocks.append(updated)
            else:
                blocks.append(block)
        content = blocks
    return message.model_copy(update={"content": content, "tool_calls": calls})
