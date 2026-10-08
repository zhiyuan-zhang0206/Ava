"""Fresh persisted compact application proof; graph return and summary success are insufficient."""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.hooks.compact import compose_summary_message
from agent.state import CompactState, ContextReset
from base.agents.compaction.execution import CompactCommand
from base.agents.compaction.models import CompactHeldError, CompactMarker
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.kwargs import AvaMsgType


def cold_reader(saver: AsyncPostgresSaver) -> AsyncPostgresSaver:
    """A fresh persisted delta reconstruction, without runtime/pending-write caches."""
    reader = AsyncPostgresSaver(saver.conn, serde=saver.serde)
    wrap_saver_reads_with_delta_reconstruction(reader)
    return reader


def marker_for(command: CompactCommand) -> CompactMarker:
    if command.result is None or command.execution is None or command.attempt_id is None:
        raise CompactHeldError("compact marker requires original execution and durable result")
    return CompactMarker(
        acceptance=command.acceptance,
        execution=command.execution,
        attempt_id=command.attempt_id,
        result_digest=command.result.digest(),
        segment_version=command.acceptance.target.segment_version + 1,
    )


async def cold_application(saver: AsyncPostgresSaver, command: CompactCommand) -> str | None:
    """Read committed materialized channels, excluding buffer and pending writes."""
    reader = cold_reader(saver)
    snapshot = await reader.aget_tuple(
        {"configurable": {"thread_id": str(command.acceptance.target.source.agent_id)}}
    )
    if snapshot is None:
        return None
    values = snapshot.checkpoint["channel_values"]
    if values.get("native_compact") is None:
        return None
    marker = CompactMarker.model_validate(values["native_compact"])
    if marker != marker_for(command):
        return None
    if not _materialized(values, command, marker):
        return None
    messages = values["messages"]
    source = await reader.aget_tuple(
        {
            "configurable": {
                "thread_id": str(command.acceptance.target.source.agent_id),
                "checkpoint_id": command.acceptance.target.checkpoint_id,
            }
        }
    )
    if source is None:
        return None
    old_ids = {message.id for message in source.checkpoint["channel_values"].get("messages", [])}
    if any(message.id in old_ids for message in messages):
        return None
    return snapshot.checkpoint["id"]


def _materialized(values: dict[str, Any], command: CompactCommand, marker: CompactMarker) -> bool:
    reset = ContextReset.model_validate(values.get("context_reset", {}))
    compact = CompactState.model_validate(values.get("compact", {}))
    if (
        values.get("halted") is not True
        or values.get("turn_idle") is not True
        or values.get("turn_active") is not False
        or reset.tail
        or compact.version != marker.segment_version
        or NativeWorkTarget.model_validate(values.get("native_work")) != marker.execution
    ):
        return False
    return _replacement_head(values.get("messages", []), command)


def _replacement_head(messages: list[Any], command: CompactCommand) -> bool:
    result = command.result
    if result is None or not messages or not isinstance(messages[0], SystemMessage):
        return False
    summary = messages[-1]
    return (
        isinstance(summary, HumanMessage)
        and summary.id == str(result.message_id)
        and summary.content == compose_summary_message(result.summary)
        and summary.additional_kwargs.get("ava_compact_id") == str(command.acceptance.command_id)
        and all(
            isinstance(message, HumanMessage)
            and message.additional_kwargs.get("ava_msg_type") != AvaMsgType.INBOUND.value
            for message in messages[1:-1]
        )
    )
