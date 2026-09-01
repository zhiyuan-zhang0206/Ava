"""Typed contract for the `ava_*` metadata carried on a LangChain message's
`additional_kwargs`.

LangChain forces `additional_kwargs` to be a plain `dict`, so the bag cannot be
a pydantic model; a `TypedDict` (total=False — every key is contextual to the
message kind) is the contract. `AvaMsgType` is the discriminator the read side
dispatches on, and `read_ava_kwargs` is the single convergence point that gives
a message's kwargs the typed view.

Writers live in `agent/messages.py` (+ `agent/graph/_claim.py`, `_llm.py`);
readers in `shared/timeline.py`, `gateway/context_breakdown.py`,
`agent/graph/_memory_recall.py`. It sits in `shared/` (leaf) so both the agent
and the gateway import it without an agent <-> gateway package cycle.

Not exhaustive of `additional_kwargs`: third-party keys ride there too — e.g.
the community `ChatMoonshot` package writes `reasoning_content` — and is
deliberately outside this contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict, cast

from langchain_core.messages import BaseMessage


class AvaMsgType(StrEnum):
    """The `ava_msg_type` discriminator on a framework-authored message's
    `additional_kwargs`. The read side (timeline / context breakdown / memory
    recall) dispatches on it. A message with no `ava_msg_type` is a plain
    conversational message (or pre-tag historical data) and falls to each
    reader's catch-all.

    This is the *vocabulary*, not the stored type: writers persist `<member>.value`
    (a plain `str`) and readers compare `== AvaMsgType.X`. The value must stay a
    plain string because LangGraph's checkpoint msgpack serializes an Enum member
    as a custom type (a persisted-format change plus a deprecation warning on
    load), so `AvaMessageKwargs` types the field as `str`, mirroring storage."""

    ATTACH = "attach"
    INBOUND = "inbound"
    SYSTEM_NOTE = "system_note"
    EXEC_OUTPUT = "exec_output"
    COMPACT_SUMMARY = "compact_summary"
    COMPACT_REQUEST = "compact_request"


class NoteTag(StrEnum):
    """Category of a framework-injected system note, carried in
    additional_kwargs['ava_note_tag'] beside ava_msg_type='system_note'.

    The timeline read side passes the tag through as the system_marker
    `source`, which the UI maps to a chip. A closed set: a new note kind adds a
    member here AND a matching UI branch — an unmapped tag renders as a loud
    'unrecognized' marker rather than silently as a generic note.

    The lifecycle_* members are process-lifecycle transitions (terminate /
    restart-complete / resurrect / fork identity); the rest are one-time
    guidance notes surfaced before the agent's next turn. `task` marks a
    task-system notification (assign / update / reminder) delivered through
    the inbound queue and claimed into a note, like `heartbeat`.
    `heartbeat_pause` is retained to render historical heartbeat-pause notes.
    """

    SDK_HINT = "sdk_hint"
    AGENT_REPLY = "agent_reply"
    TASK = "task"
    COMPACT_REMINDER = "compact_reminder"
    HISTORY_DUMP = "history_dump"
    SILENT_IDLE_CONTINUE = "silent_idle_continue"
    MEMORY = "memory"
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
    PROJECT_SKILLS = "project_skills"
    PRELOADED_SKILLS = "preloaded_skills"
    NEW_SKILLS = "new_skills"
    EXEC_TIMEOUT = "exec_timeout"
    TIMEZONE = "timezone"


class AvaMessageKwargs(TypedDict, total=False):
    """The `ava_*` metadata bag on a message's `additional_kwargs`. Every key is
    contextual to the message kind (total=False): an `inbound` carries source /
    inbound_id / image_urls, an `exec_output` carries exit_code / exec_ms, a
    `system_note` carries note_tag, an AIMessage carries the reasoning timings.

    `ava_msg_type` / `ava_note_tag` are typed `str` (not `AvaMsgType` / `NoteTag`)
    to mirror what is persisted: the value is a plain string (`<member>.value`),
    because a raw Enum member would take LangGraph's checkpoint msgpack custom-type
    path. The enums are the write/compare vocabulary, not the storage type — so
    the contract cannot invite a future write of a member that would break
    serialization.

    `ava_reasoning_ms` is legacy (a single turn-level value on the first
    thinking block); new turns write the per-block `ava_reasoning_ms_by_block`
    map. Both are read back for backward-compatible timeline rendering.
    """

    ava_msg_type: str
    ava_source: str
    ava_inbound_id: int
    ava_created_at: str
    ava_image_urls: list[str]
    ava_note_tag: str
    ava_task_id: int
    ava_exit_code: int
    ava_cancelled: bool
    ava_timed_out: bool
    ava_exec_ms: int | None
    ava_reasoning_ms_by_block: dict[str, int]
    ava_code_ms_by_block: dict[str, int]
    ava_reasoning_ms: int


def read_ava_kwargs(msg: BaseMessage) -> AvaMessageKwargs:
    """The typed `ava_*` view of a message's `additional_kwargs`.

    `additional_kwargs` has default_factory=dict, so it is always a dict; this
    is a typed reinterpretation (no copy, no runtime validation) — the single
    place the read side trades a raw `dict[str, Any]` for `AvaMessageKwargs`.
    Third-party keys sharing the dict are simply not surfaced by the type.
    """
    return cast("AvaMessageKwargs", message_addl_kwargs(msg))


# ── Typed accessors for the loosely-typed LangChain message members ──
#
# LangChain types `content` blocks and `additional_kwargs` / `response_metadata`
# as bare `dict` (Unknown keys), which propagates Unknown into every reader. These
# three accessors are the single narrowing points — one scoped ignore each, so the
# call sites in timeline / stop / reasoning-compat / labeler stay annotation-clean.


def message_content(msg: BaseMessage) -> str | list[str | dict[str, Any]]:
    """`msg.content` with its content-list block dicts retyped `dict[str, Any]`."""
    # langchain types content-list blocks as bare dict, whose Unknown keys leak.
    return cast("str | list[str | dict[str, Any]]", msg.content)  # pyright: ignore[reportUnknownMemberType]


def message_addl_kwargs(msg: BaseMessage) -> dict[str, Any]:
    """`msg.additional_kwargs` as a plain `dict[str, Any]` (langchain types it bare)."""
    return cast("dict[str, Any]", msg.additional_kwargs)  # pyright: ignore[reportUnknownMemberType]


def message_response_metadata(msg: BaseMessage) -> dict[str, Any]:
    """`msg.response_metadata` as a plain `dict[str, Any]` (langchain types it bare)."""
    return cast("dict[str, Any]", msg.response_metadata)  # pyright: ignore[reportUnknownMemberType]
