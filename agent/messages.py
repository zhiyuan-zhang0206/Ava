"""Ava-flavored message constructors — build plain LangChain HumanMessage /
ToolMessage with `additional_kwargs` metadata.

The read side (timeline endpoint / hooks etc.) uniformly classifies via
`read_ava_kwargs(msg).get("ava_msg_type")` — **not isinstance**, so no need to
subclass. The metadata keys + the `ava_msg_type` / `ava_note_tag` value sets are
the typed contract in `shared/message_kwargs.py` (`AvaMessageKwargs` TypedDict +
`AvaMsgType` / `NoteTag` StrEnums); these helpers centralize the writes.

Convention for adding a new metadata type:
    1. helper function name `<purpose>_message`
    2. add the discriminator to `AvaMsgType` and stuff `ava_msg_type: <member>.value`
       so the read side dispatches
    3. metadata keys use the `ava_` prefix (to avoid colliding with other framework
       metadata) and are declared in `AvaMessageKwargs`

Serialization: LangGraph PostgresSaver msgpack goes through standard
langchain message classes, automatically entering SAFE_MSGPACK_TYPES
allowlist — does not trigger deserialize warning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from shared.message_kwargs import AvaMsgType, NoteTag, read_ava_kwargs

# The header prepended to every replacement compact summary (forced / command /
# spontaneous) — written by `agent.hooks.compact.compose_summary_message`, and
# the one invariant the read side (gateway/context_breakdown.py) classifies the
# untagged summary HumanMessage by. It lives here, in the leaf message-contract
# module, so the gateway can import it without pulling in agent.hooks.compact
# (whose agent.graph imports do not resolve inside a gateway process).
# Two jobs:
#   1. "just compacted" — the compaction is the one event the post-compact context
#      has no surviving record of: REMOVE_ALL wipes the turn that ran it, including
#      the `[system halt] You just called ava.self.compact` ack that announced it.
#      Without this line the agent re-reads a /compact still sitting in the
#      summary's verbatim tail as a pending order and runs it again, every turn
#      (the agent-17 self-compact loop). Stating it happened is the standing signal
#      the wiped ack cannot be.
#   2. "your own prior context" — frames the first-person "I" in the body as the
#      agent's own memory, not the user speaking (the summary lands as a user-role
#      message).
COMPACT_SUMMARY_HEADER = (
    "[system] Your context was just compacted. The following is the summary of "
    "your own prior context:"
)


def _stamp_created_at(kwargs: dict[str, object], created_at: datetime | None) -> dict[str, object]:
    """Add the ISO-8601 `ava_created_at` when a real creation time is known.
    The timeline read side prefers it over its synthetic anchor+offset ordering;
    omitting it (created_at None) leaves the message on the legacy fallback path."""
    if created_at is not None:
        kwargs["ava_created_at"] = created_at.isoformat()
    return kwargs


def inbound_message(
    *,
    content: str | list[dict[str, Any]],
    source: str,
    inbound_id: int,
    created_at: datetime | None = None,
    image_urls: list[str] | None = None,
) -> HumanMessage:
    """Envelope-wrapped inbound message (product of `shared/envelope.py:wrap_inbound`).

    `content` is a plain string for a text inbound, or a list of content blocks
    for a multimodal one (a leading text block carrying the envelope-wrapped
    text, followed by native `image` blocks the model decodes). `image_urls`
    then lists each image's reference url so the timeline can render a thumbnail
    without decoding the base64 that sits in `content`.

    additional_kwargs:
        ava_msg_type: "inbound"
        ava_source: original source string ("system" / "agent:N" / "user")
        ava_inbound_id: inbound_messages.id of the source row — startup
            reconciliation reads this back from state.messages to confirm
            whether a 'claimed' inbound's commit actually landed (see
            agent/db.py:reconcile_claimed_inbounds + claim_inbound_batch).
        ava_created_at: ISO-8601 wall-clock the inbound entered the conversation
            (the source row's stored created_at). Omitted when not supplied.
        ava_image_urls: reference urls of any inlined images (timeline render).
            Omitted when the inbound carries no image.
    """
    kwargs: dict[str, object] = {
        "ava_msg_type": AvaMsgType.INBOUND.value,
        "ava_source": source,
        "ava_inbound_id": inbound_id,
    }
    if image_urls:
        kwargs["ava_image_urls"] = image_urls
    # langchain types content as str | list[str | dict]; our block list is a
    # subset it accepts at runtime (a leading text block + native image blocks).
    return HumanMessage(
        content=cast("str | list[str | dict[str, Any]]", content),
        additional_kwargs=_stamp_created_at(kwargs, created_at),
    )


def system_note_message(
    *, content: str, tag: NoteTag, task_id: int | None = None, created_at: datetime | None = None
) -> HumanMessage:
    """A framework-injected, system-styled note added to the conversation.
    Covers one-time guidance notes (SDK-primitive hints, the agent-reply
    pointer, the compact reminder, memory recall) and process-lifecycle
    transitions (terminate / restart-complete / resurrect / fork identity) —
    `tag` distinguishes which. Not tied to an inbound row.

    Distinguished from inbound_message by `ava_msg_type='system_note'`; the
    timeline read side renders it as a system_marker keyed on `tag`. Carries no
    inbound id and does not advance the chat anchor. `tag` is a NoteTag —
    pyright statically catches a wrong/typo'd value.

    additional_kwargs:
        ava_msg_type: "system_note"
        ava_note_tag: the NoteTag value (drives the timeline chip)
        ava_task_id: task attribution for a task-driven turn. Omitted otherwise.
        ava_created_at: ISO-8601 wall-clock the note was injected. Omitted when
            not supplied.
    """
    kwargs: dict[str, object] = {
        "ava_msg_type": AvaMsgType.SYSTEM_NOTE.value,
        "ava_note_tag": tag.value,
    }
    if task_id is not None:
        kwargs["ava_task_id"] = task_id
    return HumanMessage(
        content=f"[system] {content}",
        additional_kwargs=_stamp_created_at(kwargs, created_at),
    )


def attach_message(
    *, blocks: list[dict[str, object]], text: str, created_at: datetime
) -> HumanMessage:
    """Add files prepared during the preceding turn to the conversation.

    The message is available to your next response. Content blocks are
    interleaved per file — ``[text(notice), text(line1), media1, text(line2),
    media2, ...]`` — so every media block sits directly after its own caption
    line (the timeline reads that pairing structurally via
    ``shared/timeline._attach_image_captions``).
    """
    content_blocks = blocks or [{"type": "text", "text": text}]
    return HumanMessage(
        content=cast("str | list[str | dict[str, Any]]", content_blocks),
        additional_kwargs=_stamp_created_at({"ava_msg_type": AvaMsgType.ATTACH.value}, created_at),
    )


def exec_output_message(
    *,
    content: str,
    tool_call_id: str,
    exit_code: int,
    cancelled: bool = False,
    timed_out: bool = False,
    exec_ms: int | None = None,
    created_at: datetime | None = None,
) -> ToolMessage:
    """Envelope-wrapped stdout/stderr block after subprocess exec completes
    (product of `agent/graph/_exec.py:wrap_code_output`).

    Under the single-tool execute_code wire, uses the ToolMessage role to
    pair with the previous round's AIMessage.tool_calls (otherwise the
    server reports "An assistant message with 'tool_calls' must be followed
    by tool messages").

    additional_kwargs:
        ava_msg_type: "exec_output"
        ava_exit_code: int (0 = continue / 42 = idle/terminate / 43 = compact / -1 = cancelled/timeout)
        ava_cancelled: bool (set True on user cancel path)
        ava_timed_out: bool (set True on timeout path)
        ava_exec_ms: int (wall-clock the code ran; surfaced on the code_output
            timeline item so the collapsed chip can read "ran in 1.3s")
        ava_created_at: ISO-8601 wall-clock the output was produced. Omitted when
            not supplied.
    """
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        additional_kwargs=_stamp_created_at(
            {
                "ava_msg_type": AvaMsgType.EXEC_OUTPUT.value,
                "ava_exit_code": exit_code,
                "ava_cancelled": cancelled,
                "ava_timed_out": timed_out,
                "ava_exec_ms": exec_ms,
            },
            created_at,
        ),
    )


def _tail_inbound_sources(messages: list[AnyMessage]) -> list[str]:
    """The `ava_source` of every inbound added since the last assistant turn.

    Scans backward from the end, stopping at the most recent AIMessage (the
    boundary of the current incoming batch), and collects the source of each
    `ava_msg_type="inbound"` HumanMessage. `inbound_message()` always sets
    ava_source (contract field), so it is indexed rather than `.get()`-ed — a
    missing source on an "inbound"-typed message surfaces as a KeyError instead
    of being silently dropped.
    """
    sources: list[str] = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            break
        if not isinstance(msg, HumanMessage):
            continue
        if read_ava_kwargs(msg).get("ava_msg_type") != AvaMsgType.INBOUND:
            continue
        # ava_source is indexed (not `.get`) off the raw bag so a missing source
        # on an "inbound"-typed message surfaces as a KeyError rather than being
        # silently dropped — see the docstring.
        source = msg.additional_kwargs["ava_source"]  # pyright: ignore[reportUnknownMemberType]
        if isinstance(source, str):
            sources.append(source)
    return sources


def tail_has_agent_inbound(messages: list[AnyMessage]) -> bool:
    """Whether the messages added since the last assistant turn include an
    inbound from another agent (`ava_source` starts with "agent:").

    The read-side counterpart to `inbound_message`, so it lives here rather than
    in any one plugin. Used by before_llm hooks that must coordinate the single
    `messages` write a pass allows (the hook runner is fail-loud on co-writes):
    a turn woken by an agent inbound is claimed by the agent-reply reminder, so
    other note-injecting hooks defer on it.
    """
    return any(s.startswith("agent:") for s in _tail_inbound_sources(messages))


def has_conversation(messages: list[AnyMessage]) -> bool:
    """Whether anything has been *said* in this context window yet.

    A window is never truly empty past `init_context`: that node lays down the
    standing head — the SystemMessage plus the context notes behind it — before
    claim ever runs. So "nothing has happened yet" is not `not messages`; it is
    the absence of anything that is not part of that head.

    Claim uses this to tell a fresh window from a multi-step loop in progress.
    Getting it wrong in the permissive direction costs a wasted LLM turn on an
    empty conversation.
    """
    return any(
        not isinstance(m, SystemMessage)
        and read_ava_kwargs(m).get("ava_msg_type") != AvaMsgType.SYSTEM_NOTE
        for m in messages
    )


def tail_has_recallable_inbound(messages: list[AnyMessage]) -> bool:
    """Whether the messages added since the last assistant turn include an
    inbound passive memory recall should fire on: any source except a
    background wake-up (`watcher:` / `shell:` prefixes).

    The mirror of `tail_has_agent_inbound`. Used by the memory-recall hook as
    its positive gate: recall serves turns that carry real content — user chat,
    a peer agent's message (`agent:`), a scheduled turn (`schedule:`), or a
    system notice — while watcher and shell completions are machine-originated
    wake-ups whose payload is the notice itself, not conversation to recall
    over. This same tail shape (an inbound sits after the last AIMessage) makes
    it mutually exclusive with hooks that fire on a bare AIMessage tail (e.g.
    silent-idle).
    """
    return any(not s.startswith(("watcher:", "shell:")) for s in _tail_inbound_sources(messages))
