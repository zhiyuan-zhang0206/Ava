"""Qualify checkpoint message identities without creating another ID owner."""

from typing import Any, cast

from langchain_core.messages import RemoveMessage, convert_to_messages
from langchain_core.messages.utils import message_chunk_to_message
from langgraph._internal._typing import MISSING
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Overwrite

from base.agents.messages.kwargs import read_ava_kwargs


def normalize_stored_message_ids(value: Any) -> Any:
    """Mark IDs missing in stored data before a reducer can synthesize a UUID.

    This is a read/replay boundary, never a working-copy/new-delta boundary.
    The reserved metadata survives later checkpoint writes: persisting a
    reconstructed UUID must not upgrade its original source identity.
    Existing IDs, including existing markers, are preserved. LangGraph remains
    the sole ID generator; this function only records provenance.
    """
    if value is MISSING or value is None:
        return value
    if isinstance(value, _DeltaSnapshot):
        return _DeltaSnapshot(normalize_stored_message_ids(value.value))
    if isinstance(value, Overwrite):
        return Overwrite(normalize_stored_message_ids(value.value))
    group = cast("list[Any]", value) if isinstance(value, list) else [value]
    messages = [message_chunk_to_message(m) for m in convert_to_messages(group)]
    for message in messages:
        if message.id is None and not isinstance(message, RemoveMessage):
            read_ava_kwargs(message)["ava_ephemeral_message_id"] = True
    return messages


def normalize_checkpoint_message_ids(tuple_: CheckpointTuple) -> None:
    """Normalize stored seed and pending message writes at the shared read door."""
    values = tuple_.checkpoint["channel_values"]
    if "messages" in values:
        values["messages"] = normalize_stored_message_ids(values["messages"])
    if tuple_.pending_writes is not None:
        tuple_.pending_writes[:] = [
            (task, channel, normalize_stored_message_ids(value) if channel == "messages" else value)
            for task, channel, value in tuple_.pending_writes
        ]
