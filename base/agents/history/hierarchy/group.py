"""Upper-level grouping — the prompt, the plain-text reply, its checks and the correction conversation.

Level k of the understanding tree is grouped into level k+1 by one LLM call per
check (`group_consumer.py` decides when). The input is the level's open nodes
(no parent), listed by id with their time range and text, oldest first. Like a
chunk call, the model answers in plain text, one element per group it closes:
`<group first="ID" last="ID">summary</group>`; no tool, no structured output, no
provider-specific path. Code parses and checks the reply; one that breaks a rule is
sent back in the same conversation with the exact problem, up to the configured
number of corrections, then the check fails:

- groups start at the oldest open node and follow each other without a gap;
- the reply is well formed: every `<group` tag is a complete `<group ...>...</group>` element
  (a missing close tag would swallow the next group into the previous summary);
- `last` is not before `first`, and every id is one of the listed nodes;
- the newest open node is never in a group: the last group stays open;
- group sizes are the model's: no minimum, no maximum;
- a reply with no `<group>` is a legal answer (the topic is still going), unless the caller
  says the open set has grown past three checks' worth (`must_close`, set by
  `group_consumer.py`): then at least one group must be closed — the only brake on an
  open set that keeps growing.

Every provider call, the refused replies and failed calls included, is handed to
`on_call` for the raw record. The prompt states only the purpose and the rules —
no language, length, audience or notion of what matters.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from base.agents.history.hierarchy.generate import UNDERSTANDING_RETRY_ATTEMPTS, GenerateError
from base.clock import Clock
from base.lm.call import invoke_response

GROUP_ENGINE_VERSION = "group-0.1"
GROUP_PROMPT_VERSION = "group-0.8"

_PROMPT = """Below are {count} consecutive summaries of an AI agent's work, oldest first, each \
with an id and its time range. Group consecutive summaries about the same matter and summarize \
each group one level higher, shorter than the summaries it covers put together; its reader can always \
open the members below it. This is a view \
one level above the summaries: a group may span a wider matter than the matter of any one \
summary below it, not only summaries about exactly the same thing.

Groups start at the oldest summary and follow one another without gaps. Leave the newest summary \
out of every group; you may also leave out the last few if their matter is still going on. No \
groups at all is a fine answer.{must_close}

Reply with the groups in order, each as <group first="ID" last="ID">summary</group>, ID being \
the id of the group's oldest and newest summary. Do not call any tool.

{nodes}"""

_MUST_CLOSE = " Here at least one group must be closed."

_CORRECTION = """Your reply cannot be used: {problem}

Reply again with the corrected groups, in the same format. Do not call any tool."""

# Only a tag with its attributes opens a group: a summary that mentions the word `<group` is fine.
_OPENING = re.compile(r"<group\s+first=")
_GROUP = re.compile(
    r'<group\s+first="?(\d{1,9})"?\s+last="?(\d{1,9})"?\s*>(.*?)</group>', re.DOTALL
)


@dataclass(frozen=True)
class OpenNode:
    """One open node of the level being grouped."""

    id: int
    span_start: int
    span_end: int
    start: datetime
    end: datetime
    text: str


@dataclass(frozen=True)
class Group:
    """One closed group: its oldest and newest child ids, and the summary."""

    first: int
    last: int
    summary: str


class GroupReplyError(Exception):
    """A reply breaks a rule; the message is what the model is told."""


@dataclass(frozen=True)
class GroupCall:
    """The raw record of one provider call of a grouping check.

    `request` is the message sent this round (the prompt, or the correction);
    `problem` is why the reply was refused (None = accepted); `response` is
    None for a failed call, whose `error` is set.
    """

    round: int
    model: str
    request: str
    response: Any | None
    duration_ms: float
    problem: str | None
    error: str | None


def build_group_prompt(nodes: Sequence[OpenNode], *, clock: Clock, must_close: bool) -> str:
    """The request: purpose, rules, envelope, then the open nodes by id."""
    listing = "\n\n".join(
        f"[id {n.id}] {clock.format_timestamp(n.start)} to {clock.format_timestamp(n.end)}\n{n.text}"
        for n in nodes
    )
    return _PROMPT.format(
        count=len(nodes),
        must_close=_MUST_CLOSE if must_close else "",
        nodes=listing,
    )


def parse_groups(text: str, nodes: Sequence[OpenNode], *, must_close: bool) -> list[Group]:
    """The groups of a reply that close, checked against the open `nodes`; a trailing one-summary group stays open.

    Raises:
        GroupReplyError: a grouping rule is broken, or no group closed when `must_close`.
    """
    groups = [Group(int(a), int(b), body.strip()) for a, b, body in _GROUP.findall(text)]
    opened = len(_OPENING.findall(text))
    if opened != len(groups):
        raise GroupReplyError(
            f"the reply opens {opened} <group> tags but holds {len(groups)} complete groups: "
            "close every group with </group>, and no summary contains the tag"
        )
    _check_groups(groups, nodes)
    # A group of one summary at the END of the reply stays open: it joins the next check with the
    # newer nodes. One in the middle or at the start is closed like any other: the open nodes
    # must stay one contiguous run from the oldest, so a closed group never spans an open node
    # (a parent over a node that stayed open would overlap the parent that later covers it).
    # A reply of only such groups under `must_close` closes nothing and is refused like an empty one.
    while groups and groups[-1].first == groups[-1].last:
        groups = groups[:-1]
    if not groups and must_close:
        raise GroupReplyError(
            f"there are {len(nodes)} open summaries, so at least one group must be closed"
        )
    return groups


def _check_groups(groups: Sequence[Group], nodes: Sequence[OpenNode]) -> None:
    position = {n.id: i for i, n in enumerate(nodes)}
    expected_start = 0
    for group in groups:
        for name, value in (("first", group.first), ("last", group.last)):
            if value not in position:
                raise GroupReplyError(f"{name}={value} is not one of the listed ids")
        lo, hi = position[group.first], position[group.last]
        if not group.summary:
            raise GroupReplyError(f"the group {group.first}..{group.last} has an empty summary")
        if lo != expected_start:
            raise GroupReplyError(
                f"the group starting at id {group.first} should start at id "
                f"{nodes[expected_start].id}: groups start at the oldest summary and follow "
                "one another without gaps"
            )
        if hi < lo:
            raise GroupReplyError(
                f"the group {group.first}..{group.last} ends before it starts: "
                "last is the newest summary of the group"
            )
        expected_start = hi + 1
    if groups and expected_start >= len(nodes):
        raise GroupReplyError(
            f"the group ending at id {groups[-1].last} includes the newest summary; "
            "the newest summary stays open"
        )


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1_000


def _record(on_call: Callable[[GroupCall], None] | None, call: GroupCall) -> None:
    if on_call is not None:
        on_call(call)


def generate_groups(
    llm: Any,
    nodes: Sequence[OpenNode],
    *,
    model: str,
    corrections: int,
    clock: Clock,
    must_close: bool = False,
    retry_attempts: int = UNDERSTANDING_RETRY_ATTEMPTS,
    on_call: Callable[[GroupCall], None] | None = None,
) -> list[Group]:
    """Ask the model to close groups over `nodes`; blocking (run it in a worker thread).

    A refused reply is answered in the same conversation with its problem, up to
    `corrections` more calls. `on_call` sees every provider call, the failed one included.

    Raises:
        GenerateError: the provider call failed after retries, or the reply was
            still refused after the last correction.
        Exception: an unknown model invocation error, unchanged and without retry.
    """
    request = build_group_prompt(nodes, clock=clock, must_close=must_close)
    messages: list[Any] = [HumanMessage(content=request)]
    for round_no in range(corrections + 1):
        started = time.monotonic()
        try:
            response = invoke_response(
                llm,
                messages,
                desc=f"{model}, understanding group",
                error_type=GenerateError,
                retry_attempts=retry_attempts,
                model=model,
                usage_source="hierarchy.group",
            )
        except Exception as exc:
            _record(
                on_call, GroupCall(round_no, model, request, None, _ms(started), None, str(exc))
            )
            raise
        try:
            groups = parse_groups(response.text, nodes, must_close=must_close)
        except GroupReplyError as exc:
            problem = str(exc)
            _record(
                on_call,
                GroupCall(round_no, model, request, response, _ms(started), problem, None),
            )
            if round_no == corrections:
                raise GenerateError(
                    f"grouping reply refused after {corrections} correction(s): {problem}"
                ) from exc
            request = _CORRECTION.format(problem=problem)
            messages = [*messages, response, HumanMessage(content=request)]
            continue
        _record(on_call, GroupCall(round_no, model, request, response, _ms(started), None, None))
        return groups
    raise AssertionError("unreachable: the last round returns or raises")  # pragma: no cover
