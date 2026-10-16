from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict, cast

from base.lm.content import message_addl_kwargs

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


class AvaMsgType(StrEnum):
    ATTACH = "attach"
    INBOUND = "inbound"
    SYSTEM_NOTE = "system_note"
    EXEC_OUTPUT = "exec_output"
    COMPACT_SUMMARY = "compact_summary"
    COMPACT_REQUEST = "compact_request"


class ExecStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    HALTED = "halted"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


# ava:ref base/agents/messages/docs/metadata-contracts.ava.okf.md#notetag-closed-set
class NoteTag(StrEnum):
    IMPERSONATION = "impersonation"
    SDK_HINT = "sdk_hint"
    AGENT_REPLY = "agent_reply"
    TASK = "task"
    COMPACT_REMINDER = "compact_reminder"
    HISTORY_DUMP = "history_dump"
    SILENT_IDLE_CONTINUE = "silent_idle_continue"
    MEMORY = "memory"
    MEMORY_WRITE_CHECK = "memory_write_check"
    LIFECYCLE_TERMINATE = "lifecycle_terminate"
    LIFECYCLE_RESTART = "lifecycle_restart"
    LIFECYCLE_RESURRECT = "lifecycle_resurrect"
    LIFECYCLE_FORK = "lifecycle_fork"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_PAUSE = "heartbeat_pause"
    SECURITY = "security"
    CONTEXT = "context"
    AGENT_ID = "agent_id"
    AGENT_MEMORY = "agent_memory"
    INHERITED_MEMORY = "inherited_memory"
    PROJECT_SKILLS = "project_skills"
    PRELOADED_SKILLS = "preloaded_skills"
    NEW_SKILLS = "new_skills"
    SKILL_RECALL = "skill_recall"
    EXEC_TIMEOUT = "exec_timeout"
    TIMEZONE = "timezone"


class AvaUsage(TypedDict, total=False):
    model: str
    in_total: int
    out_total: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    reasoning: int
    cost_usd: float
    price_miss: float
    price_hit: float
    price_out: float
    price_write_5m: float
    price_write_1h: float
    unpriced: int


class AvaMessageKwargs(TypedDict, total=False):
    ava_ephemeral_message_id: bool
    ava_impersonation: dict[str, Any]
    ava_msg_type: str
    ava_source: str
    ava_inbound_id: int
    ava_inbound_body_start: int
    ava_created_at: str
    ava_picked_up_at: str
    ava_compact_id: str
    ava_image_urls: list[str]
    ava_note_tag: str
    ava_task_id: int
    ava_exec_ms: int | None
    ava_exec_status: str
    ava_exec_started_at: str | None
    ava_exec_body_start: int
    sdk_calls: list[dict[str, Any]] | None
    ava_reasoning_ms_by_block: dict[str, int]
    ava_code_ms_by_block: dict[str, int]
    ava_usage: AvaUsage


def read_ava_kwargs(msg: BaseMessage) -> AvaMessageKwargs:
    return cast("AvaMessageKwargs", message_addl_kwargs(msg))


def kwargs_read_time(kwargs: AvaMessageKwargs) -> str | None:
    return kwargs.get("ava_picked_up_at") or kwargs.get("ava_created_at")


def message_read_time(msg: BaseMessage) -> str | None:
    return kwargs_read_time(read_ava_kwargs(msg))


# ava:ref base/agents/messages/docs/metadata-contracts.ava.okf.md#compact-summary-header-roles
COMPACT_SUMMARY_HEADER = (
    "[system] Your context was just compacted. The following is the summary of "
    "your own prior context:"
)
