"""The `agent.messages` constructors store the discriminator as a plain `str`.

A `StrEnum` member would send the checkpoint serializer down its Enum custom-type path."""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.messages import exec_output_message, inbound_message, system_note_message
from base.agents.messages.kwargs import AvaMsgType, NoteTag


def test_stored_discriminator_is_plain_str() -> None:
    """Constructors store `ava_msg_type` / `ava_note_tag` as plain `str`, never
    the StrEnum member — the serialization-safety invariant."""
    inbound = inbound_message(content="hi", source="user", inbound_id=1)
    note = system_note_message(content="n", tag=NoteTag.MEMORY)
    exec_out = exec_output_message(content="ok", tool_call_id="t1")

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


def test_exec_output_sdk_calls_present_empty_and_omitted() -> None:
    """The exec_output's `sdk_calls` (the run's real SDK-call tally) is written
    when known — `[]` stays a present, real zero — and omitted when unknown."""
    sized = exec_output_message(
        content="ok",
        tool_call_id="t1",
        sdk_calls=[{"method": "files.read", "count": 3}],
    )
    assert sized.additional_kwargs["sdk_calls"] == [{"method": "files.read", "count": 3}]  # pyright: ignore[reportUnknownMemberType]

    empty = exec_output_message(content="ok", tool_call_id="t2", sdk_calls=[])
    assert empty.additional_kwargs["sdk_calls"] == []  # pyright: ignore[reportUnknownMemberType]

    unknown = exec_output_message(content="ok", tool_call_id="t3")
    assert "sdk_calls" not in unknown.additional_kwargs  # pyright: ignore[reportUnknownMemberType]


def test_exec_output_sdk_calls_survive_checkpoint_roundtrip() -> None:
    """The metadata is plain JSON and survives the checkpoint serializer as-is."""
    serde = JsonPlusSerializer()
    msg = exec_output_message(
        content="ok",
        tool_call_id="t1",
        sdk_calls=[{"method": "files.read", "count": 3}],
    )
    restored = serde.loads_typed(serde.dumps_typed(msg))
    assert restored.additional_kwargs["sdk_calls"] == [{"method": "files.read", "count": 3}]  # pyright: ignore[reportUnknownMemberType]


def test_exec_output_carries_no_outcome_status() -> None:
    """Outcome (ok / error / timeout / cancelled) lives in the content text only."""
    msg = exec_output_message(
        content="Code execution output [timeout after 60s]:\n\nx", tool_call_id="t1"
    )
    assert set(msg.additional_kwargs) == {"ava_msg_type", "ava_exec_ms"}  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
