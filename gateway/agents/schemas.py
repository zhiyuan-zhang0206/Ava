"""Wire models of the agent surface: the selected-agent row, threads and
inbound messages, notices, and the per-agent token-usage / context-breakdown
reads.

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from base.agents import AgentStatus
from base.agents.messages.envelope import validate_writable_source
from base.agents.messages.kwargs import NoteTag
from base.agents.observation.snapshot import AgentSnapshot
from base.agents.tasks.priority import Priority
from ops.rpc_schemas.content import UserContent


class AgentRow(AgentSnapshot):
    """GET /api/agents/{id} detail, including response-required notice bodies.

    The directory and live roster use bounded cards from base.agents.observation.roster.
    last_active_at is the real-activity clock; last_inbound_at is the latest
    inbound message clock.
    """


class NewThread(BaseModel):
    """POST /api/agents response."""

    id: int


class LabelPatchRequest(BaseModel):
    """PATCH /api/agents/{id} body — manually set / reset agent label.

    `label=""` resets back to NULL; the frontend re-displays fallback
    `#N`. Non-empty strings are stripped; length 1-64 inclusive. `source` names who
    changed it in the `label_change` audit fact: the operator by default, `"self"` when
    the agent sets its own label.
    """

    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]
    source: Literal["user", "self"] = "user"


class CompactEnqueued(BaseModel):
    """POST /api/agents/{id}/compact response — returns immediately after
    pending insert, does not wait for the kernel loop to finish."""

    agent_id: int
    status: Literal["enqueued"]
    inbound_id: int | None = None


class UserMessageIn(BaseModel):
    """POST /api/agents/{id}/messages request body — user message to agent.

    Constraints at the schema layer: blank / content over one million
    characters returns 422 from pydantic; the endpoint does not 400 anymore.
    """

    content: UserContent


class MessageEnqueued(BaseModel):
    """POST /api/agents/{id}/messages response."""

    agent_id: int
    status: Literal["enqueued"]


class CancelRequest(BaseModel):
    """POST /api/cancel request body — pause/stop the agent, addressed by id."""

    agent_id: int = Field(..., gt=0)


class AgentMessageEnqueued(BaseModel):
    """POST /messages receipt — durable inbound id plus current agent status.

    Same-key retries return the same `inbound_id`; `status` may be recomputed
    because auto-resurrection can advance while the client reconciles.
    """

    status: AgentStatus
    inbound_id: int | None = None


class SystemNoteIn(BaseModel):
    """POST /api/agents/{id}/system-note request body — a framework system
    notification delivered to the agent as a system note (system_marker in
    the timeline), never a peer chat message.

    `content` is the note text. `source` follows the same envelope whitelist
    as chat messages (system / agent:N / user / ...), defaulting to 'system';
    it is recorded for audit but the note itself renders without a sender
    prefix. `note_tag` picks the timeline chip — must be a NoteTag value
    (closed set); `task` is the task-notification family. `resurrect`
    controls whether a terminated target is woken to receive the note: task
    assignment (a delegator direction) resurrects, plain update notices do
    not (user ruling 2026-08-27 — notifications never resurrect an owner).
    `task_id` is optional but requires `note_tag='task'`; it is the explicit
    LLM-usage attribution for the turn this note drives.
    """

    content: str = Field(min_length=1)
    source: str = Field(default="system", min_length=1, max_length=64)
    note_tag: str = "task"
    task_id: int | None = Field(default=None, gt=0)
    resurrect: bool = True

    @field_validator("note_tag")
    @classmethod
    def _check_note_tag(cls, v: str) -> str:
        try:
            NoteTag(v)
        except ValueError as exc:
            raise ValueError(f"note_tag {v!r} is not a NoteTag value") from exc
        return v

    @field_validator("source")
    @classmethod
    def _check_envelope_source(cls, v: str) -> str:
        validate_writable_source(v)
        return v

    @model_validator(mode="after")
    def _check_task_attribution(self) -> "SystemNoteIn":
        if self.task_id is not None and self.note_tag != NoteTag.TASK.value:
            raise ValueError("task_id requires note_tag='task'")
        return self


class ResolveNoticeIn(BaseModel):
    """POST /api/agents/{id}/notices/{notice_id}/resolve request body.

    `action` is the explicit close verb (never inferred from whether `reply` is
    present): `answer` / `dismiss` apply to a require_response notice, `read`
    applies to an FYI notice. `reply` is the user's free-text reply — required
    for `answer`, optional for `dismiss` and `read`. When present it is cached on
    the notice row and delivered back to the agent as a normal chat inbound.
    """

    action: Literal["answer", "dismiss", "read"]
    reply: UserContent | None = None


class NoticeCreateIn(BaseModel):
    """POST /api/agents/{id}/notices request body — the unified notice write API
    (R3 door ④).

    Mirrors the SDK ava.ui.notify() contract: `priority` P0-P3, `blocking`
    meaningful only when `require_response` is true (an FYI never stalls).
    `task_id` optionally groups the notice under a task in the user's queue.
    `expire_at` specifies the notice lifetime; omitted uses the configured default TTL.
    """

    title: str
    content: str | None = None
    priority: Priority = Priority.P2
    require_response: bool = False
    blocking: bool = False
    task_id: int | None = None
    expire_at: datetime | None = None


class NoticeEditIn(BaseModel):
    """PATCH /api/agents/{id}/notices/current request body — revise the agent's
    open notice (the SDK ava.ui.edit_notice contract). All fields optional;
    pass only what changes. `require_response` cannot be changed (turn an FYI
    into a question by dismissing and posting fresh)."""

    title: str | None = None
    content: str | None = None
    priority: Priority | None = None
    blocking: bool | None = None


class NoticeItem(BaseModel):
    """One agent_notices row — element of GET /api/notices/open (the FYI feed:
    require_response false, resolved_at None) and the resolved page of
    GET /api/notices (the cross-fleet resolution history). Joined to the agent label.

    Served as an independent feed kept off the agent snapshot — the snapshot
    carries the open require_response notices inline + an unread FYI count, so a
    large FYI backlog never bloats the fleet-wide broadcast.

    `resolution` is NULL while open, else one of answered / dismissed / read /
    withdrawn / superseded; `reply` is the cached user reply text (NULL when none).
    """

    id: int
    agent_id: int
    agent_label: str | None
    title: str
    content: str | None
    priority: Priority
    require_response: bool
    blocking: bool
    created_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    reply: str | None = None
    # The task this notice belongs to, or None — lets the feed group by task.
    task_id: int | None = None
    expire_at: datetime


class NoticesCursor(BaseModel):
    """Keyset cursor for the resolved-history page of GET /api/notices —
    a (resolved_at, id) pair. Supplied together or not at all;
    `before_at` is the last row's resolution time, `before_id` its id."""

    before_at: datetime
    before_id: int


class NoticesFeed(BaseModel):
    """GET /api/notices — the unified inbox feed (Task #1024, R4 layer 2,
    decision Q1=A). One request carries the whole Inbox panel: the OPEN
    queue split by kind (open = FYI notices that need no response, awaiting
    = require_response notices that ride the agent snapshot today) plus one
    keyset page of the RESOLVED history.

    The contract shape was chosen so a panel = one request = one hook:
    the client no longer merges three independent pipes (agent snapshot +
    /api/notices/open + the resolved history). `next_cursor` is None when
    `resolved_page` is the last page (short page or exhausted); pass it back
    as before_at/before_id for the next strictly-older page."""

    open: list[NoticeItem]
    awaiting: list[NoticeItem]
    resolved_page: list[NoticeItem]
    next_cursor: NoticesCursor | None


class PendingInbound(BaseModel):
    """One queued inbound (status='pending', kind='chat') — element of
    GET /api/agents/{id}/pending.

    These have not been claimed by the agent yet, so they are absent from
    the timeline snapshot; the web UI shows them as a compact strip above
    the composer. `source` disambiguates origin (user / agent:N /
    watcher:N / ...) so the UI can label who queued it.

    `images` carries the image reference urls of a multimodal inbound (None
    when the message has no image) — the same gateway-relative upload urls
    the timeline renders for a claimed message, so the strip shows
    thumbnails with one contract.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    source: str | None
    content: str
    images: list[str] | None
    created_at: datetime


class AgentMessagesResponse(BaseModel):
    """GET /api/agents/{id}/messages response — the raw, unrendered
    state.messages for programmatic consumers (ops scripts / other agents /
    evals). The rendered, frontend-facing view is GET .../timeline; this is
    the data layer beneath it.

    `messages[i]` is one LangChain BaseMessage `model_dump()` (raw fields:
    type / content / tool_calls / id / additional_kwargs / ...) and
    corresponds to `state.messages[start_index + i]`. `msg_count` is the
    total length of state.messages; `start_index` is the absolute index of
    the first returned message. `has_more` reports whether older messages
    exist; when true, `start_index` is the exclusive `before` cursor for the
    next older page.
    """

    messages: list[dict[str, Any]]
    msg_count: int
    start_index: int
    has_more: bool


class LastMessageResponse(BaseModel):
    """GET /api/agents/{id}/last-message response — the text of the last
    AI message, or None when no AI message with text content exists yet.
    """

    text: str | None


class TraceCheckpointMessagesResponse(BaseModel):
    """GET /api/agents/{id}/traces/{trace_id}/messages response — the full
    message list (incl. the system prompt) of the turn that produced one OTel
    trace, resolved on demand from the checkpoints table.

    Spans are metadata-only (trace v2): this endpoint is how content comes
    back. `pruned` distinguishes the two absence shapes:
    - pruned=true, checkpoint_id=None — the trace's checkpoint was dropped by
      compact/checkpoint trim (retention is the latest K checkpoints), so the
      content is gone; the span metadata in the mirror still exists. The
      frontend renders this as "trimmed" rather than an error.
    - pruned=false — the checkpoint exists; messages is its `messages` channel
      (the full conversation at that point, system prompt included).

    `messages[i]` is one LangChain BaseMessage `model_dump()` — the same raw
    shape as GET /api/agents/{id}/messages.
    """

    trace_id: str
    agent_id: int
    checkpoint_id: str | None
    pruned: bool
    messages: list[dict]


class TokenUsageResponse(BaseModel):
    """GET /api/agents/{id}/token-usage response — token usage of the
    most recent LLM call. `input_tokens` is context-window occupancy.

    Real-time deltas rely on the SSE `token_usage` event; when
    switching back to an existing agent, the frontend uses this endpoint
    to fetch the historical last value to initialize the UI (SSE is
    fire-and-forget with no persistence). Returns `(0, 0, 0)` if no
    AIMessage in state.messages carries usage_metadata (new agent /
    has not run any LLM call yet). `reasoning_tokens` is the
    reasoning/thinking portion of output (gemini/openai reasoning
    models + Anthropic thinking); defaults to 0 when absent.

    `soft_compact_tokens` / `hard_compact_tokens` are the agent model's
    wind-down-reminder and force-compact thresholds (a fraction of
    `max_input_tokens`, resolved per agent), so the composer can render the two
    marks against current occupancy without a second request. Both default to 0
    when the agent's model has no known context window.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0


class ContextSection(BaseModel):
    """One node of the system prompt's recursive section breakdown: a heading — or
    the `(preamble)` / `(intro)` prose before the first sub-heading — with its
    token share of the system prompt (always estimated: a section is never measured alone). A section over ~1000 tokens is drilled into its
    next-level sub-headings as `children` (recursively); a smaller section, or one
    with no deeper heading, is a leaf (`children == []`). When `children` is
    non-empty their tokens sum to this node's `tokens`."""

    name: str
    tokens: int
    estimated: bool = True
    children: list["ContextSection"] = Field(default_factory=list)


class ContextCategory(BaseModel):
    """One context bucket (system_prompt / compact_summary / cluster_memory /
    agent_memory / context_note / user_input / agent_messages / automation /
    reasoning / output / tool_call / tool_response) with its token count: the sum of its
    messages' own counts. `estimated` is true when any part was a share of a provider total
    rather than the provider's own number (the UI appends "(estimated)"); `exact_fraction` is the
    share of `tokens` that was exact. Inbound messages split by source: user_input (a human
    turn), agent_messages (a peer agent), automation (a machine/framework wakeup)."""

    kind: str
    tokens: int
    estimated: bool
    exact_fraction: float


class ContextBreakdownResponse(BaseModel):
    """GET /api/agents/{id}/context-breakdown — how the agent's context window is
    spent, for the composer's breakdown panel (lazy-loaded when the panel opens).

    The breakdown of the latest LLM request's input. Each message's tokens are anchored to
    the provider's reported `input_tokens` (see `base/agents/history/message_tokens.py`), so
    `categories` sum exactly to `total_input_tokens`; only the inside of a message
    (reasoning / output / tool_call, system-prompt sections) is split by an estimator.
    `estimated` / `exact_fraction` say whether any part of the total was estimated and how much
    of it was the provider's own number. `sections` break down the `system_prompt` category.
    With no LLM request yet the total is 0 and nothing is listed. `max_input_tokens` /
    `soft_compact_tokens` / `hard_compact_tokens` mirror the token-usage endpoint (0 when the
    model window is unknown)."""

    total_input_tokens: int
    estimated: bool
    exact_fraction: float
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0
    sections: list[ContextSection]
    categories: list[ContextCategory]
