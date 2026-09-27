"""execute_code tool_call normalization.

The model occasionally emits multiple `tool_use` blocks in the same
AIMessage. Ava's execution semantics is still a single `execute_code`:
multiple code snippets are concatenated into one Python snippet in original
order, keeping the first tool_call id, to avoid the next round's provider
rejecting the whole history due to missing tool_result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.messages import AIMessage, ToolCall

from shared.log import logger


@dataclass(frozen=True)
class _ToolPiece:
    id: str
    name: str
    code: str


def code_from_args(args: Any, *, source: str) -> str:
    """The `code` argument of a tool call, strictly. Raises if `args` is not a
    dict or `code` is present but not a string; returns "" only when `code` is
    absent. The single strict extractor the exec path (merge) and the llm-node
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


def _piece_from_tool_call(tool_call: ToolCall) -> _ToolPiece:
    tool_id = tool_call["id"] or ""
    name = tool_call["name"]
    return _ToolPiece(
        id=tool_id,
        name=name,
        code=code_from_args(tool_call["args"], source=f"tool_call {tool_id!r}"),
    )


def _content_tool_use_blocks(message: AIMessage) -> list[dict[str, Any]]:
    content: Any = message.content  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(content, list):
        return []
    blocks = cast(list[Any], content)
    return [
        cast(dict[str, Any], block)
        for block in blocks
        if isinstance(block, dict) and cast(dict[str, Any], block).get("type") == "tool_use"
    ]


def _code_from_tool_use_block(block: dict[str, Any]) -> str:
    block_id = block["id"]
    input_value = block.get("input")
    if isinstance(input_value, dict) and "code" in input_value:
        return code_from_args(input_value, source=f"content tool_use {block_id!r}.input")

    partial_json = block.get("partial_json")
    if isinstance(partial_json, str) and partial_json:
        parsed = json.loads(partial_json)
        return code_from_args(parsed, source=f"content tool_use {block_id!r}.partial_json")

    return ""


def _tool_pieces_in_llm_order(message: AIMessage) -> list[_ToolPiece]:
    tool_calls: list[ToolCall] = list(message.tool_calls)
    calls_by_id = {tool_call["id"]: tool_call for tool_call in tool_calls}
    content_blocks = _content_tool_use_blocks(message)

    if not content_blocks:
        return [_piece_from_tool_call(tool_call) for tool_call in tool_calls]

    pieces: list[_ToolPiece] = []
    seen: set[str] = set()
    for block in content_blocks:
        block_id = block["id"]
        name = block["name"]
        tool_call = calls_by_id.get(block_id)
        code = (
            code_from_args(tool_call["args"], source=f"tool_call {block_id!r}")
            if tool_call is not None
            else _code_from_tool_use_block(block)
        )
        pieces.append(_ToolPiece(id=block_id, name=name, code=code))
        seen.add(block_id)

    for tool_call in tool_calls:
        tool_id = tool_call["id"]
        if tool_id not in seen:
            pieces.append(_piece_from_tool_call(tool_call))
    return pieces


def _replace_content_tool_use(
    content: str | list[Any],
    *,
    tool_call: dict[str, Any],
    code: str,
) -> str | list[Any]:
    if not isinstance(content, list):
        return content

    new_content: list[Any] = []
    replaced = False
    for block in content:
        if not isinstance(block, dict) or cast(dict[str, Any], block).get("type") != "tool_use":
            new_content.append(block)
            continue
        if replaced:
            continue
        new_block = dict(cast(dict[str, Any], block))
        new_block["id"] = tool_call["id"]
        new_block["name"] = tool_call["name"]
        new_block["input"] = {"code": code}
        new_block["partial_json"] = json.dumps({"code": code}, ensure_ascii=False)
        new_content.append(new_block)
        replaced = True
    return new_content


def replace_single_execute_code(message: AIMessage, code: str) -> AIMessage:
    """Replace the code of a single tool_call, while synchronizing the tool_use block in content."""
    if len(message.tool_calls) != 1:
        raise ValueError(
            f"replace_single_execute_code requires 1 tool_call, got {len(message.tool_calls)}"
        )

    tool_call = dict(message.tool_calls[0])
    raw_args = tool_call.get("args")
    if raw_args is None:
        args: dict[str, Any] = {}
    elif isinstance(raw_args, dict):
        args = dict(cast(dict[str, Any], raw_args))
    else:
        raise TypeError(
            f"tool_call {tool_call['id']!r}.args must be dict, got {type(raw_args).__name__}"
        )
    args["code"] = code
    tool_call["args"] = args

    return message.model_copy(
        update={
            "content": _replace_content_tool_use(message.content, tool_call=tool_call, code=code),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "tool_calls": [tool_call],
            "invalid_tool_calls": [],
        }
    )


def merge_multiple_execute_code_tool_calls(
    message: AIMessage,
    *,
    agent_id: int,
    location: str,
) -> AIMessage | None:
    """Multiple `execute_code` tool_calls → single tool_call.

    Returns None if the message is already single-tool_call or has no tool_call;
    returns AIMessage if normalized, the caller should apply same-id message
    replacement to write back to state.
    """
    pieces = _tool_pieces_in_llm_order(message)
    if len(pieces) <= 1:
        return None

    first = pieces[0]
    tool_calls: list[dict[str, Any]] = [dict(tool_call) for tool_call in message.tool_calls]
    calls_by_id = {tool_call["id"]: tool_call for tool_call in tool_calls}
    first_tool_call = calls_by_id.get(first.id)
    if first_tool_call is None:
        merged_tool_call: dict[str, Any] = {"id": first.id, "name": first.name, "args": {}}
    else:
        merged_tool_call = dict(first_tool_call)
    merged_tool_call["id"] = first.id
    merged_tool_call["name"] = first.name
    merged_tool_call["args"] = {"code": "\n\n".join(piece.code for piece in pieces)}

    merged = message.model_copy(update={"tool_calls": [merged_tool_call], "invalid_tool_calls": []})
    merged = replace_single_execute_code(merged, merged_tool_call["args"]["code"])
    ids = [piece.id for piece in pieces]
    logger.info(
        "[{label}] {body}",
        label="tool-call-merge",
        body=f"{location}: merged {len(pieces)} execute_code tool calls into first id={first.id!r}",
        event="multiple_tool_calls_merged",
        agent_id=agent_id,
        tool_call_ids=ids,
    )
    return merged
