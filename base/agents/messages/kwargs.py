"""Typed contract for the `ava_*` metadata carried on a LangChain message's
`additional_kwargs`.

LangChain forces `additional_kwargs` to be a plain `dict`, so the bag cannot be
a pydantic model; a `TypedDict` (total=False — every key is contextual to the
message kind) is the contract. `AvaMsgType` is the discriminator the read side
dispatches on, and `read_ava_kwargs` is the single convergence point that gives
a message's kwargs the typed view.

Writers live in `agent/messages/__init__.py` (+ `agent/graph/claim/node.py`, `agent/graph/llm/node.py`);
readers in `base/agents/history/timeline.py`, `base/agents/history/context_breakdown.py`,
`agent/graph/recall/memory_recall.py`. It sits in `base/` (leaf) so both the agent
and the gateway import it without an agent <-> gateway package cycle.

Not exhaustive of `additional_kwargs`: third-party keys ride there too — e.g.
the community `ChatMoonshot` package writes `reasoning_content` — and is
deliberately outside this contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    # Annotation-only on the read helpers; callers always pass real LangChain
    # messages, but this leaf module sits on the provider-registration import
    # path, which must stay off the message stack (exec-child boot, task
    # #3633; `_TYPE_CHECKING_ALLOWED`).
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

    IMPERSONATION = "impersonation"
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
    INHERITED_MEMORY = "inherited_memory"
    PROJECT_SKILLS = "project_skills"
    PRELOADED_SKILLS = "preloaded_skills"
    NEW_SKILLS = "new_skills"
    EXEC_TIMEOUT = "exec_timeout"
    TIMEZONE = "timezone"


class AvaUsage(TypedDict, total=False):
    """`ava_usage` on an agent turn's final AIMessage: the figures of the `llm_usage` event
    emitted at the same moment, from the same `quote`.

    `cost_usd` and the `price_*` rates (USD per 1M tokens) are the usage-time snapshot; an
    unpriced call carries `unpriced: 1` and none of them. `in_total` includes `cache_read`
    and the cache writes. A message without this key predates it: its cost is unknown,
    never estimated.
    """

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
    """The `ava_*` metadata bag on a message's `additional_kwargs`. Every key is
    contextual to the message kind (total=False): an `inbound` carries source /
    inbound_id / image_urls, an `exec_output` carries exec_ms /
    sdk_calls, a `system_note` carries note_tag, a compact summary carries
    `ava_compact_id`, an AIMessage carries the reasoning timings. `sdk_calls`
    is the one framework key without the `ava_` prefix — the frozen wire name
    for the exec_output's runtime SDK-call tally (`agent/graph/exec/node.py` writes
    it; the timeline projection reads it back).

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

    ava_ephemeral_message_id: bool
    ava_impersonation: dict[str, Any]
    ava_msg_type: str
    ava_source: str
    ava_inbound_id: int
    ava_created_at: str
    ava_picked_up_at: str
    ava_compact_id: str
    ava_image_urls: list[str]
    ava_note_tag: str
    ava_task_id: int
    ava_exec_ms: int | None
    sdk_calls: list[dict[str, Any]] | None
    ava_reasoning_ms_by_block: dict[str, int]
    ava_code_ms_by_block: dict[str, int]
    ava_reasoning_ms: int
    ava_usage: AvaUsage


def read_ava_kwargs(msg: BaseMessage) -> AvaMessageKwargs:
    """The typed `ava_*` view of a message's `additional_kwargs`.

    `additional_kwargs` has default_factory=dict, so it is always a dict; this
    is a typed reinterpretation (no copy, no runtime validation) — the single
    place the read side trades a raw `dict[str, Any]` for `AvaMessageKwargs`.
    Third-party keys sharing the dict are simply not surfaced by the type.
    """
    return cast("AvaMessageKwargs", message_addl_kwargs(msg))


def kwargs_read_time(kwargs: AvaMessageKwargs) -> str | None:
    """`message_read_time` over an already-read kwargs view."""
    return kwargs.get("ava_picked_up_at") or kwargs.get("ava_created_at")


def message_read_time(msg: BaseMessage) -> str | None:
    """ISO wall-clock the message entered the LLM context, or None for legacy.

    `ava_picked_up_at` when present (injected messages: inbound, notes,
    attachments), else `ava_created_at` (AIMessage and tool output are produced
    inside the context, so their creation IS their read time; messages
    persisted before `ava_picked_up_at` existed fall back to it too). Readers
    that order or display by when the model read a message use this, never
    `ava_created_at` directly.
    """
    return kwargs_read_time(read_ava_kwargs(msg))


# The header prepended to every replacement compact summary (forced / command /
# spontaneous) — written by `agent.hooks.compact.compose_summary_message`, and
# the one invariant the read side (base/agents/history/context_breakdown.py) classifies the
# untagged summary HumanMessage by. It lives here, in the leaf message-contract
# module, so the gateway and the insights service can import it without pulling in
# agent.hooks.compact (whose agent.graph imports do not resolve outside the agent).
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
