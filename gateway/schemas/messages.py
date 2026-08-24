"""threads / inbound messages / notices.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from ops.rpc_schemas import _UserContent
from shared.agents import AgentStatus
from shared.priority import Priority


class NewThread(BaseModel):
    """POST /api/agents response."""

    id: int


class LabelPatchRequest(BaseModel):
    """PATCH /api/agents/{id} body — manually set / reset agent label.

    `label=""` resets back to NULL; the frontend re-displays fallback
    `#N`. Non-empty strings are stripped; length 1-64 inclusive.
    """

    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]


class CompactEnqueued(BaseModel):
    """POST /api/agents/{id}/compact response — returns immediately after
    pending insert, does not wait for the kernel loop to finish."""

    mode: Literal["framework", "agent"]
    agent_id: int
    status: Literal["enqueued"]


class UserMessageIn(BaseModel):
    """POST /api/agents/{id}/messages request body — user message to agent.

    Constraints at the schema layer: blank / content over one million
    characters returns 422 from pydantic; the endpoint does not 400 anymore.
    """

    content: _UserContent


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


class ResolveNoticeIn(BaseModel):
    """POST /api/agents/{id}/notices/{notice_id}/resolve request body.

    `action` is the explicit close verb (never inferred from whether `reply` is
    present): `answer` / `dismiss` apply to a require_response notice, `read`
    applies to an FYI notice. `reply` is the user's free-text reply — required
    for `answer`, optional for `dismiss` and `read`. When present it is cached on
    the notice row and delivered back to the agent as a normal chat inbound.
    """

    action: Literal["answer", "dismiss", "read"]
    reply: _UserContent | None = None


class NoticeCreateIn(BaseModel):
    """POST /api/agents/{id}/notices request body — the unified notice write API
    (R3 door ④).

    Mirrors the SDK ava.ui.notify() contract: `priority` P0-P3, `blocking`
    meaningful only when `require_response` is true (an FYI never stalls).
    `task_id` optionally groups the notice under a task in the user's queue.
    """

    title: str
    content: str | None = None
    priority: Priority = Priority.P2
    require_response: bool = False
    blocking: bool = False
    task_id: int | None = None


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
    require_response false, resolved_at None) and GET /api/notices/resolved (the
    cross-fleet resolution history). Joined to the agent label.

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


class EscalationNoticeItem(BaseModel):
    """One open task escalation for GET /api/notices/escalations.

    The operator queue joins the escalation notice to its task and current
    owner, so its consumer can decide whether to reassign, cancel, or retain
    the task without fetching another resource.
    """

    id: int
    title: str
    priority: Priority
    created_at: datetime
    task_id: int
    task_title: str
    task_status: str
    owner_id: int | None
    owner_label: str | None
    reminder_count: int
    updated_at: datetime


class NoticesCursor(BaseModel):
    """Keyset cursor for the resolved-history page of GET /api/notices —
    mirror of the (resolved_at, id) cursor the standalone
    /api/notices/resolved endpoint accepts. Supplied together or not at all;
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
    /api/notices/open + /api/notices/resolved). `next_cursor` is None when
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
    """

    model_config = ConfigDict(frozen=True)

    id: int
    source: str | None
    content: str
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
    the first returned message (0 for a full / un-windowed read). Together
    they let a paging consumer place the window without inferring offsets.
    """

    messages: list[dict[str, Any]]
    msg_count: int
    start_index: int


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
      content is gone; the span metadata in the mirror / events table still
      exists. The frontend renders this as "trimmed" rather than an error.
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
