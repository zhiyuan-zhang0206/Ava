"""Chunk description generation — the instruction and the agent-shaped model call.

A chunk's request is the agent's own conversation up to the chunk's end
(byte-identical prefix: the provider serves it from cache) plus one trailing
instruction message. The instruction opens with a line that sets it apart from the
messages before it, then asks the model to divide the stretch's layer-0 units (`units.py`)
into consecutive groups and summarize each, and ends with a numbered catalog of the units,
one line each: `[number] type: content` (`build_catalog`; a framework-injected note shows a
short label instead). Every group is named by the numbers of its first and last unit
(`leaf_groups.py`; code looks the number up, nothing is matched against text), and every unit is
in a group: a topic that crosses the chunk's end is two nodes, joined by the upper levels. The
instruction states what the catalog is, what a summary is for (a node a level above the raw
messages, much shorter, in the conversation's language) and the reply envelope, and prescribes
nothing else — no audience, no notion of what matters, nor how many groups or how large.

The model is built the way the agent builds its own (the caller passes it;
`generate.build_generation_llm`), the tool schema stays bound for cache parity,
and a tool-call response is refused and re-invoked (`generate._invoke_agent_shaped`).
A reply whose numbers cannot be resolved goes back in the same conversation (tools
still bound, so the request keeps the agent's cache prefix) with every problem found, and
only the groups are asked again, up to the correction budget; past it the call
fails with `GenerateError`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from base.agents.history.hierarchy.chunks import ChunkCall
from base.agents.history.hierarchy.generate import (
    UNDERSTANDING_RETRY_ATTEMPTS,
    GenerateError,
    GenParams,
    ModelCall,
    _invoke_agent_shaped,
)
from base.agents.history.hierarchy.group import GroupReplyError
from base.agents.history.hierarchy.leaf_groups import UnitGroup, parse_reply, resolve_groups
from base.agents.history.hierarchy.units import MessageUnit, catalog_line, divide_units
from base.lm.catalog import ModelCatalog

_CHUNK_PROMPT = """The messages above are background; what follows is a separate task.

Divide the conversation part listed in the catalog below into consecutive groups, keeping \
consecutive units about the same matter together, and summarize each group. The catalog is the \
complete, ordered list of that part: refer to units by their number; there is no need to match \
them against the messages above. The summaries describe only the listed units; there is no \
need to confirm what happened outside them.

The part is made of units: each inbound message is a unit, each piece of text the agent \
outputs is a unit, and the agent's reasoning, tool calls and the results of those calls \
together form a unit. A catalog line starts with its unit's type: the sender of an inbound \
message (human message, agent N message, watcher N, ...), agent text, or work (the start of its \
reasoning, call and output, in that order). A line in parentheses is a framework-injected message: \
join it to a neighbouring group; it needs no summary of its own.

Each summary is a node in a tree one level above the raw messages, which its reader sees right \
below it; it is not a handoff that has to stand on its own: say at a higher level what happened \
in the group, much shorter than the messages it covers. Write in the same language as the \
conversation.

Reply with the groups in order, each as <group first="N" last="M">summary</group>, N and M being \
the catalog numbers of its first and last unit. Do not call any tool.

Catalog (one unit per line; a group is a run of whole units):
{catalog}"""

_CORRECTION = """Your groups cannot be used:
{problem}

Reply again with all the groups, in the same format. Do not call any tool."""


def build_catalog(chunk: Sequence[BaseMessage], units: Sequence[MessageUnit]) -> str:
    """The numbered catalog of a chunk's units: `[number] start-of-unit`, one per line.

    The text is `units.catalog_line`: the unit's type, then its content (for work the start of its
    reasoning, call and output), or a parenthesized label for a framework-injected note.
    """
    return "\n".join(f"[{n}] {catalog_line(unit, chunk)}" for n, unit in enumerate(units, 1))


def build_chunk_instruction(chunk: Sequence[BaseMessage], units: Sequence[MessageUnit]) -> str:
    """The trailing instruction for a chunk (`units` = `divide_units(chunk)`): the ask and the
    numbered catalog of the units.

    Raises:
        ValueError: `chunk` or `units` is empty.
    """
    if not chunk or not units:
        raise ValueError("a chunk has at least one message and one unit")
    return _CHUNK_PROMPT.replace("{catalog}", build_catalog(chunk, units))


@dataclass(frozen=True)
class ChunkResult:
    """A chunk's groups: the stretch's units and the groups resolved over them (every unit is
    in one)."""

    units: list[MessageUnit]
    groups: list[UnitGroup]


def generate_chunk(
    llm: Any,
    prefix: Sequence[BaseMessage],
    start_offset: int,
    *,
    model: str,
    catalog: ModelCatalog,
    agent_id: int,
    tools: Sequence[Any],
    corrections: int = 0,
    params: GenParams | None = None,
    retry_attempts: int = UNDERSTANDING_RETRY_ATTEMPTS,
    on_call: Callable[[ChunkCall], None] | None = None,
) -> ChunkResult:
    """Group and describe `prefix[start_offset:]`; blocking (run it in a worker thread).

    `agent_id` is the agent whose history it is; the calls' usage is attributed to it.
    `corrections` is the budget of `<groups>` re-asks in the same conversation. `on_call`
    receives the raw record of every provider call, the failed one included; a call whose
    groups were refused carries the reason in `problem`.

    Raises:
        GenerateError: the provider call failed after retries, the model kept calling tools,
            or its groups were still refused after the corrections.
        ValueError: the stretch has no unit.
    """
    chunk = list(prefix[start_offset:])
    units = divide_units(chunk)
    instruction = build_chunk_instruction(chunk, units)
    catalog_lines = build_catalog(chunk, units).splitlines()
    buffer: list[ChunkCall] = []
    rounds = 0

    def ask(messages: list[Any], sent: str, kind: str) -> tuple[str, Any, int]:
        """One `_invoke_agent_shaped` request; its text, final response and that call's row index."""
        nonlocal rounds
        first = rounds
        final_rows: list[int] = []

        def record(call: ModelCall) -> None:
            nonlocal rounds
            buffer.append(
                ChunkCall(
                    round=first + call.round,
                    model=model,
                    instruction=sent,
                    prefix_len=len(prefix),
                    start_offset=start_offset,
                    response=call.response,
                    duration_ms=call.duration_ms,
                    error=call.error,
                    kind=kind,
                )
            )
            rounds = first + call.round + 1
            if call.response is not None:
                final_rows.append(len(buffer) - 1)

        text = _invoke_agent_shaped(
            llm,
            messages,
            catalog=catalog,
            tools=tools,
            desc=f"{model}, understanding chunk",
            model=model,
            retry_attempts=retry_attempts,
            params=params or GenParams(),
            agent_id=agent_id,
            on_call=record,
        )
        row = final_rows[-1] if final_rows else -1
        return text, buffer[row].response if final_rows else None, row

    try:
        messages: list[Any] = [*prefix, HumanMessage(content=instruction)]
        text, response, row = ask(messages, instruction, "leaf")
        for attempt in range(corrections + 1):
            try:
                groups = resolve_groups(parse_reply(text), units, catalog_lines)
                return ChunkResult(units, groups)
            except GroupReplyError as exc:
                problem = str(exc)
                buffer[row] = replace(buffer[row], problem=problem)
                if attempt == corrections:
                    raise GenerateError(
                        f"grouping reply refused after {corrections} correction(s): {problem}"
                    ) from exc
            correction = _CORRECTION.format(problem=problem)
            messages = [*messages, response, HumanMessage(content=correction)]
            text, response, row = ask(messages, correction, "group-correction")
        raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover
    finally:
        if on_call is not None:
            for call in buffer:
                on_call(call)
