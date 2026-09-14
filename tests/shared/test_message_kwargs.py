"""Contract tests for the typed `ava_*` message-metadata layer
(`shared/message_kwargs.py`).

The load-bearing invariant here is serialization safety: the message
constructors must store the discriminator / note-tag as a **plain `str`**, not
the `StrEnum` member. LangGraph's checkpoint msgpack serializer special-cases
`Enum` (encoding it as a registered custom type and emitting a deprecation
warning on load), so storing a member would silently change the persisted
format for every tagged message. These tests lock the plain-string storage
against that regression.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.messages import exec_output_message, inbound_message, system_note_message
from shared.message_kwargs import AvaMsgType, NoteTag, read_ava_kwargs


def test_msg_type_value_set() -> None:
    """The discriminator value set is the wire contract — lock it explicitly."""
    assert {t.value for t in AvaMsgType} == {
        "attach",
        "inbound",
        "system_note",
        "exec_output",
        "compact_summary",
        "compact_request",
    }


def test_note_tag_value_set() -> None:
    """The system-note tag set is a cross-stack wire contract."""
    assert {tag.value for tag in NoteTag} == {
        "sdk_hint",
        "agent_reply",
        "task",
        "impersonation",
        "compact_reminder",
        "history_dump",
        "silent_idle_continue",
        "memory",
        "lifecycle_terminate",
        "lifecycle_restart",
        "lifecycle_resurrect",
        "lifecycle_fork",
        "heartbeat",
        "heartbeat_pause",
        "security",
        "context",
        "agent_id",
        "agent_memory",
        "inherited_memory",
        "project_skills",
        "preloaded_skills",
        "new_skills",
        "exec_timeout",
        "timezone",
    }


def test_stored_discriminator_is_plain_str() -> None:
    """Constructors store `ava_msg_type` / `ava_note_tag` as plain `str`, never
    the StrEnum member — the serialization-safety invariant."""
    inbound = inbound_message(content="hi", source="user", inbound_id=1)
    note = system_note_message(content="n", tag=NoteTag.MEMORY)
    exec_out = exec_output_message(content="ok", tool_call_id="t1", exit_code=0)

    assert type(inbound.additional_kwargs["ava_msg_type"]) is str  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert inbound.additional_kwargs["ava_msg_type"] == AvaMsgType.INBOUND  # pyright: ignore[reportUnknownMemberType]
    assert type(note.additional_kwargs["ava_msg_type"]) is str  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert type(note.additional_kwargs["ava_note_tag"]) is str  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.MEMORY  # pyright: ignore[reportUnknownMemberType]
    assert type(exec_out.additional_kwargs["ava_msg_type"]) is str  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_checkpoint_roundtrip_no_custom_type() -> None:
    """A tagged message round-trips through the LangGraph checkpoint serializer
    with the discriminator staying a plain `str` (no Enum custom-type path)."""
    serde = JsonPlusSerializer()
    note = system_note_message(content="n", tag=NoteTag.SECURITY)
    restored = serde.loads_typed(serde.dumps_typed(note))
    tag = restored.additional_kwargs["ava_note_tag"]
    assert type(tag) is str
    assert tag == NoteTag.SECURITY


def test_read_ava_kwargs_is_live_view() -> None:
    """`read_ava_kwargs` returns the message's own additional_kwargs (typed
    reinterpretation, not a copy) so the read side and any writes share it."""
    msg = HumanMessage(content="x", additional_kwargs={"ava_msg_type": "inbound"})
    kw = read_ava_kwargs(msg)
    assert kw is msg.additional_kwargs  # pyright: ignore[reportUnknownMemberType]
    assert kw.get("ava_msg_type") == AvaMsgType.INBOUND


def test_exec_output_sdk_calls_present_empty_and_omitted() -> None:
    """The exec_output's `sdk_calls` (the run's real SDK-call tally) is written
    when known — `[]` stays a present, real zero — and omitted when unknown."""
    sized = exec_output_message(
        content="ok",
        tool_call_id="t1",
        exit_code=0,
        sdk_calls=[{"method": "files.read", "count": 3}],
    )
    assert sized.additional_kwargs["sdk_calls"] == [{"method": "files.read", "count": 3}]  # pyright: ignore[reportUnknownMemberType]

    empty = exec_output_message(content="ok", tool_call_id="t2", exit_code=0, sdk_calls=[])
    assert empty.additional_kwargs["sdk_calls"] == []  # pyright: ignore[reportUnknownMemberType]

    unknown = exec_output_message(content="ok", tool_call_id="t3", exit_code=0)
    assert "sdk_calls" not in unknown.additional_kwargs  # pyright: ignore[reportUnknownMemberType]


def test_exec_output_sdk_calls_survive_checkpoint_roundtrip() -> None:
    """The metadata is plain JSON and survives the checkpoint serializer as-is."""
    serde = JsonPlusSerializer()
    msg = exec_output_message(
        content="ok",
        tool_call_id="t1",
        exit_code=0,
        sdk_calls=[{"method": "files.read", "count": 3}],
    )
    restored = serde.loads_typed(serde.dumps_typed(msg))
    assert restored.additional_kwargs["sdk_calls"] == [{"method": "files.read", "count": 3}]  # pyright: ignore[reportUnknownMemberType]
