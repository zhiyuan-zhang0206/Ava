from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict, cast

from base.lm.content import message_addl_kwargs

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


class MeivaMsgType(StrEnum):
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


# meiva:note
# NoteTag is a closed cross-stack set: a new member needs its frontend dispatch branch (markers.tsx) and its gateway timestamp classification, or the UI renders the loud unrecognized marker instead of a generic note. The value set itself is a wire contract, locked by value-set tests against silent renames.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
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
    TO_USER_SOFT_LIMIT = "to_user_soft_limit"
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


class MeivaUsage(TypedDict, total=False):
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


class MeivaMessageKwargs(TypedDict, total=False):
    meiva_ephemeral_message_id: bool
    meiva_impersonation: dict[str, Any]
    meiva_msg_type: str
    meiva_source: str
    meiva_inbound_id: int
    meiva_inbound_body_start: int
    meiva_created_at: str
    meiva_picked_up_at: str
    meiva_compact_id: str
    meiva_image_urls: list[str]
    meiva_note_tag: str
    meiva_task_id: int
    meiva_exec_ms: int | None
    meiva_exec_status: str
    meiva_exec_started_at: str | None
    meiva_exec_body_start: int
    sdk_calls: list[dict[str, Any]] | None
    meiva_reasoning_ms_by_block: dict[str, int]
    meiva_code_ms_by_block: dict[str, int]
    meiva_usage: MeivaUsage


def read_meiva_kwargs(msg: BaseMessage) -> MeivaMessageKwargs:
    return cast("MeivaMessageKwargs", message_addl_kwargs(msg))


def kwargs_read_time(kwargs: MeivaMessageKwargs) -> str | None:
    return kwargs.get("meiva_picked_up_at") or kwargs.get("meiva_created_at")


def message_read_time(msg: BaseMessage) -> str | None:
    return kwargs_read_time(read_meiva_kwargs(msg))


# meiva:note
# COMPACT_SUMMARY_HEADER is prepended to every replacement compact summary and is the read side's classifier for the untagged summary HumanMessage; it is also the standing "a compaction just happened" signal, without which an agent re-reads a /compact order in the summary's verbatim tail and runs it again every turn. The string lives in the leaf message-contract module so gateway and insights callers import it without agent.graph.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
COMPACT_SUMMARY_HEADER = (
    "[system] Your context was just compacted. The following is the summary of "
    "your own prior context:"
)
