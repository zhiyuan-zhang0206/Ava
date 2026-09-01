"""SSE live projection — typed Redis pub/sub payloads for the real-time UI view.

These events are a **live projection**: they are published to the Redis
`ava:events` channel and consumed by the frontend, and are never persisted.
They are distinct from the unified events table facts
(`shared/telemetry.py` — `Event` LogRecord facts that land in the `events`
table, the durable source of truth). A live projection is a render hint for
the UI; a telemetry event is a fact. The two never cross: nothing here is
written to the DB, and the telemetry emitter does not publish to this
channel.

Redis pub/sub channel + typed event payload.

SSE live-view event window — the single source of truth for how often the
frontend can render streaming events. The agent's event publisher coalesces
emitted deltas / exec chunks into this window (one event per window), and the
frontend throttles its stream re-parse to the same window, so producer and
consumer stay aligned at ~25 FPS. Regenerate the frontend copy via
`scripts/dump_frontend_constants.py` (pre-commit drift-checks it).


SDK (ava/) and agent / UI (agent/, ui/) both publish / subscribe to
the same channel; this lives at the top level to avoid agent
triggering SDK init side effects via ava import (same rationale as
exit_codes.py).

Design: one Pydantic `BaseModel` per role; the shared `role` field
uses `Literal[...]` as discriminator. `Event` is a discriminated
union; `EVENT_ADAPTER` is used in the UI tailer to
`validate_json(raw)` deserialize.

Wire format: `{"agent_id": int, "role": str, ...}`. `EVENT_ADAPTER`
raises `ValidationError` on unknown roles — adding a role requires
syncing producer and consumer; not forward-compat.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from shared.agent_snapshot import AgentSnapshot

# One SSE event per window: the agent-side publisher coalesces deltas / chunks
# into this window and the frontend throttles stream parsing to the same value
# (see ui/web/src/lib/constants-generated.ts, generated from this constant).
EVENT_COALESCE_MS: int = 40


class _Base(BaseModel):
    """Shared field — role is overridden by subclasses with
    `Literal[...]` as the discriminator. `frozen=True` makes
    deserialized events immutable — consumers are read-only."""

    model_config = ConfigDict(frozen=True)

    agent_id: int


class ChatStart(_Base):
    """LLM started emitting text content (the model speaking to the
    user) — the UI starts a new chat block.

    `item_id` is the stable key coordinating the frontend timeline
    with the server snapshot; format `f"{msg_idx}.{block_idx}"`
    (msg_idx = AIMessage position in state.messages, block_idx =
    anthropic content_block_index). During streaming the frontend
    uses it to create items; after commit, the gateway timeline
    endpoint computes id by the same rule, matching ids on both
    sides = same logical item; on merge, the snapshot version wins,
    no more ts heuristic.
    """

    role: Literal["chat_start"] = "chat_start"
    item_id: str


class ChatDelta(_Base):
    """A small token slice of chat text — content is the delta text.
    See `item_id` in ChatStart.

    Persistence goes with the AIMessage into LangGraph
    state.messages (timeline endpoint refetch picks it up); real-time
    display relies on this event stream. The two paths are
    independent; the frontend can both stitch live and refetch the
    full content."""

    role: Literal["chat_delta"] = "chat_delta"
    item_id: str
    content: str


class CompactRequest(_Base):
    """Notification sent by the SDK right after the agent calls
    `ava.self.compact(summary)` — the UI can display
    "compacting..."; content carries a human-readable description
    (length etc.)."""

    role: Literal["compact_request"] = "compact_request"
    content: str


class CompactDone(_Base):
    """Compact finished — signal that messages were modified in place
    on the **current agent**. The UI prints one line "compact done";
    does not switch agent (agent_id is unchanged)."""

    role: Literal["compact_done"] = "compact_done"


class CodeStart(_Base):
    """LLM started emitting agent code — the UI starts a new code
    block. See `item_id` in ChatStart."""

    role: Literal["code_start"] = "code_start"
    item_id: str


class CodeDelta(_Base):
    """A small token slice within a code block — content is the
    delta text."""

    role: Literal["code_delta"] = "code_delta"
    item_id: str
    content: str


class ReasoningStart(_Base):
    """LLM started emitting thinking content (internal reasoning) —
    the UI starts a new reasoning block. See `item_id` in ChatStart."""

    role: Literal["reasoning_start"] = "reasoning_start"
    item_id: str


class ReasoningDelta(_Base):
    """A small token slice of thinking content — content is the
    delta text."""

    role: Literal["reasoning_delta"] = "reasoning_delta"
    item_id: str
    content: str


class ExecStart(_Base):
    """Code stream finished, subprocess began executing — the UI
    displays an empty code-output block that will fill with streaming
    output. `item_id` is the stable key matching the subsequent
    ExecOutputChunk / ExecOutput events so the placeholder is reused."""

    role: Literal["exec_start"] = "exec_start"
    item_id: str


class ExecOutputChunk(_Base):
    """Incremental subprocess stdout/stderr fragments pushed as the
    subprocess runs — the UI streams append-display without waiting
    for the entire exec to finish.

    Shares `item_id` with ExecOutput (the final
    wrap_code_output-enveloped full version); the frontend finds the
    code_output item by id and appends the delta. On exec completion
    ExecOutput upserts the same id and replaces it with the
    commit-version with envelope header.

    A keepalive frame has no content and is emitted at ~2Hz while the
    subprocess runs without producing output, so the frontend can show the
    exec is alive without appending text.

    `content` is text already incrementally UTF-8 decoded (subprocess
    output is bytes, multi-byte characters may cross chunk
    boundaries; `codecs.IncrementalDecoder` buffers at boundaries)."""

    role: Literal["exec_output_chunk"] = "exec_output_chunk"
    item_id: str
    content: str
    keepalive: bool = False


class ExecOutput(_Base):
    """Final exec stdout/stderr/exit code — the same blob the kernel feeds
    back to the agent and the UI displays. One shared view by design; the
    full unfiltered traceback goes to the logs for debugging.

    `item_id` format `f"{msg_idx}.0"`, msg_idx = position of
    ToolMessage(exec_output) in state.messages (exec_node returns
    Command and LangGraph appends). Matches the id the gateway
    timeline endpoint computes for the same ToolMessage; also shared
    with ExecOutputChunk (streaming fragments)."""

    role: Literal["exec_output"] = "exec_output"
    item_id: str
    content: str


class Error(_Base):
    """User-perceivable failure (graph.ainvoke raised, Compaction
    LLM failed, chat persistence FK collision etc.). The UI should
    show content and reset streaming state."""

    role: Literal["error"] = "error"
    content: str


class Cancelled(_Base):
    """User actively clicked Stop (Web UI button) or the kernel
    received a cancel signal and terminated the current turn. The UI
    resets streaming state and prints an "Aborted" line.

    Distinct from `Error`: Error is an exception failure; Cancelled
    is explicit user intent."""

    role: Literal["cancelled"] = "cancelled"


class InboundArrived(_Base):
    """New system event: any inbound message INSERTed into
    inbound_messages publishes this. The Developer view uses it to
    show external inbound arrivals in real time."""

    role: Literal["inbound_arrived"] = "inbound_arrived"
    inbound_id: int
    kind: str
    source: str
    content: str


class TokenUsage(_Base):
    """Published on LLM call completion — `input_tokens` is the
    context window the model actually saw (system prompt + all
    messages history), i.e. "how much of the context window was used".

    chunk-level input_tokens is unavailable (both Anthropic and
    OpenAI return usage_metadata only once at the end of the stream),
    so this event fires once per LLM call. The UI shows
    input_tokens as an absolute number. output_tokens is carried but
    not displayed. reasoning_tokens is the reasoning portion of
    output — present for all providers: Anthropic (via
    ThinkingTokensChatAnthropic surfacing thinking_tokens), Gemini,
    OpenAI, and DeepSeek (via anthropic-compatible endpoint); defaults
    to 0 when absent."""

    role: Literal["token_usage"] = "token_usage"
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0


class LLMDone(_Base):
    """LLM streaming completed — published by
    `RedisStreamHandler.finish()`.

    UI uses it to trigger timeline reload: if the user refreshes /
    switches agents while the agent is streaming, missing *_start
    events leaves accumulated partial items (the frontend marks
    partial and renders ellipsis + italic). When the stream
    completes, LangGraph state.messages has the commit-version
    AIMessage; the frontend receives LLMDone and triggers reload to
    fetch the full timeline overwriting partials.
    """

    role: Literal["llm_done"] = "llm_done"


class TimelineSnapshot(_Base):
    """Timeline snapshot, published by the agent at each graph node
    **enter** (`agent/graph/_node_log.node_lifecycle`), rendered from the
    node's in-memory state.messages. The gateway forwards it unchanged; the
    frontend merges it via mergeSnapshotWithStreaming.

    Snapshots are INCREMENTAL: `items` carries only the messages committed
    since the agent's last published snapshot (a per-process cursor), so the
    render cost is O(new commits) and the per-enter anchors DB query is gone.
    Full-window snapshots (the tail window of the whole history) are
    published on the first enter after process start, after a compaction
    shrink, and as the claim node's turn-end fallback. The frontend does not
    need to tell the two shapes apart on the wire: both merge by item_id
    (snapshot ids replace prev; everything else in prev is kept).

    No system-prompt special-casing: full-window snapshots carry the ~128KB
    item (0.0) when the tail window contains it (guaranteed at spawn — the
    history is short); incremental snapshots never contain it (message 0 is
    below the cursor). The frontend's id-replace merge keeps a single copy
    either way; the cold-load GET /timeline endpoint re-attaches 0.0 for
    long conversations where tail-windowing drops it.

    Rendering from in-memory state (not a checkpoint re-read) is what makes
    this race-free: LangGraph commits checkpoints asynchronously, so a re-read
    can miss the just-claimed inbound, whereas in-memory state.messages
    reflects the reducer the instant it applies.

    `msg_count` = `len(state.messages)` at publish time — ALWAYS the full
    length, never the window/incremental length (hard invariant). The
    frontend distinguishes future / committed for partial items
    `item_id = "{msg_idx}.{block_idx}"`:
    - partial.msg_idx == msg_count -> single future position
      (LLM/exec is streaming the next message), keep;
    - other partials not in the snapshot -> stale (old high msg_idx after
      compact / error-interrupted generation), drop — the frontend's narrow
      partial-cleaning rule.
    """

    role: Literal["timeline_snapshot"] = "timeline_snapshot"
    items: list[dict[str, Any]]  # list of TimelineItem.model_dump()
    msg_count: int


class InboundCommitted(_Base):
    """The claim node envelope-wrapped a chat inbound into a
    HumanMessage and wrote into LangGraph state.messages; publishes
    a protocol-layer ACK.

    Distinct from `InboundArrived`:
    - InboundArrived: gateway publishes on INSERT completion (the
      message is enqueued; the agent has not yet seen it). The
      frontend uses this as a client-side echo so the user sees
      their message immediately.
    - InboundCommitted: claim node publishes after processing (the
      message is in state.messages; GET /timeline can fetch the
      commit version). The frontend uses this as a reload trigger.

    Both are needed: without InboundArrived, the user feels no
    response after sending; without InboundCommitted, the frontend
    does not know when GET /timeline can fetch the envelope-wrapped
    version.
    """

    role: Literal["inbound_committed"] = "inbound_committed"
    inbound_id: int


class PageOpened(_Base):
    """Published when the agent calls `ava.ui.show(name)` to
    register / update an HTML page — the frontend Pages popover adds
    or in-place replaces an entry.

    `name` is a stable identifier within (agent_id); same-name
    re-register triggers another PageOpened (host/port may change, the
    frontend replaces the row in place).

    `url`: absolute gateway reverse-proxy URL
    (`http://<gateway>/pages/<id>-<name>/`) — the popover
    `<a href={url}>` opens it in a new tab; the gateway serves the content.
    """

    role: Literal["page_opened"] = "page_opened"
    page_id: int
    name: str
    port: int
    title: str | None
    url: str


class PageClosed(_Base):
    """Published when the agent calls `ava.ui.close(name)`, the terminate
    cascade closes an agent-owned show() page, or the page-server daemon closes
    a row whose serve_dir remains unavailable — the frontend Pages popover
    removes the entry.

    Terminate path: SQL trigger `cascade_close_agent_pages` silently
    marks only serve_dir-NULL show() rows closed on
    `UPDATE agents_meta.status='terminated'`; all
    three terminate entries (gateway `mark_agent_exited_op` self-exit /
    `_force_mark_terminated` zombie cleanup / gateway force=true)
    SELECT page names before the UPDATE and publish each after
    UPDATE so the popover removes entries in real time instead of
    waiting for the user to switch agents."""

    role: Literal["page_closed"] = "page_closed"
    name: str


class AgentSpawned(_Base):
    """A new agent was created (INSERT into agents + agents_meta committed).
    Carries the full snapshot so the frontend upserts into its agents list
    without re-fetching. Published once per agent, at spawn / resurrect /
    respawn paths."""

    role: Literal["agent_spawned"] = "agent_spawned"
    snapshot: AgentSnapshot


class AgentUpdated(_Base):
    """An existing agent's snapshot changed — status transition, label
    update, started_at / pid set, last_active_at advanced. Published from
    every site that UPDATEs agents_meta. The frontend setQueryData merges
    by id; the snapshot is authoritative."""

    role: Literal["agent_updated"] = "agent_updated"
    snapshot: AgentSnapshot


class LabelUpdated(_Base):
    """Thread label change notification:
    - gateway BackgroundTask LLM finished generating + CAS write
      succeeded publishes
    - PATCH /api/agents/{id} user manual edit / reset publishes
      after write

    `label=None` means reset back to "not set" (frontend fallback
    `#N`).
    """

    role: Literal["label_updated"] = "label_updated"
    label: str | None


class NoticePosted(_Base):
    """Published when the agent posts or edits any open notice via
    `ava.ui.notify` / `ava.ui.edit_notice`. Carries only the lightweight header
    (not the body), so the unified Inbox refetches its queue. A notice that needs
    a response also publishes AgentUpdated, whose snapshot carries its body and
    response state for inspector consumers."""

    role: Literal["notice_posted"] = "notice_posted"
    notice_id: int
    priority: str
    title: str
    # The task this notice belongs to, or None when it names none — lets the
    # frontend group the FYI feed by task the same way the snapshot does.
    task_id: int | None = None


class NoticeResolved(_Base):
    """Published when an open notice leaves the queue. The frontend refetches the
    open and resolved Inbox views. The resolution's write or delivery path also
    publishes AgentUpdated when it removes a response-required notice, keeping
    the inspector snapshot independent of this Inbox event."""

    role: Literal["notice_resolved"] = "notice_resolved"
    notice_id: int


class TaskCreated(_Base):
    """Published when an agent creates a task through the ava.tasks SDK. Fleet-wide
    (GLOBAL_ROLES), so every open task board invalidates and refetches /api/tasks.
    `agent_id` is the creating agent. Carries only the task id — consumers refetch
    the registry rather than upserting from the event, so no task body rides here."""

    role: Literal["task_created"] = "task_created"
    task_id: int


class TaskUpdated(_Base):
    """Published when an agent updates a task through the ava.tasks SDK (status,
    owner, priority, results, reminder). Same fleet-wide invalidation trigger as
    TaskCreated; `agent_id` is the acting agent. The gateway PATCH path is a plain
    column write with no acting agent and does not emit — the board's slow poll
    reconciles those."""

    role: Literal["task_updated"] = "task_updated"
    task_id: int


class ClusterUpdateStarted(_Base):
    """Published after a whole-cluster rollout or restart orchestration is
    successfully spawned. The frontend treats it only as a hint to reload
    through Gate, which projects the persistent marker and owns the page/clock.

    This cluster-level event has no owning agent, so ``agent_id`` is always 0.
    It is a non-authoritative reload hint and is never persisted.
    """

    role: Literal["cluster_update_started"] = "cluster_update_started"
    kind: Literal["rollout", "restart"]
    origin: str


Event = Annotated[
    ChatStart
    | ChatDelta
    | CompactRequest
    | CompactDone
    | CodeStart
    | CodeDelta
    | ReasoningStart
    | ReasoningDelta
    | ExecStart
    | ExecOutputChunk
    | ExecOutput
    | Error
    | Cancelled
    | InboundArrived
    | InboundCommitted
    | LabelUpdated
    | PageOpened
    | PageClosed
    | TokenUsage
    | LLMDone
    | TimelineSnapshot
    | AgentSpawned
    | AgentUpdated
    | NoticePosted
    | NoticeResolved
    | TaskCreated
    | TaskUpdated
    | ClusterUpdateStarted,
    Field(discriminator="role"),
]

# One registry listing every live role with its fleet-wide flag (R2-C):
# SYSTEM_ROLES / GLOBAL_ROLES and the runtime EVENT_ADAPTER derive from it, so
# adding a role means adding the class + one entry here (and the static `Event`
# union below — a bidirectional test in tests/agent/test_events_wire_format.py
# pins union == registry).
_ROLE_CLASSES: tuple[tuple[type[Event], bool], ...] = (
    (ChatStart, False),
    (ChatDelta, False),
    (CompactRequest, False),
    (CompactDone, False),
    (CodeStart, False),
    (CodeDelta, False),
    (ReasoningStart, False),
    (ReasoningDelta, False),
    (ExecStart, False),
    (ExecOutputChunk, False),
    (ExecOutput, False),
    (Error, False),
    (Cancelled, False),
    (InboundArrived, False),
    (InboundCommitted, False),
    (LabelUpdated, True),
    (PageOpened, True),
    (PageClosed, True),
    (TokenUsage, False),
    (LLMDone, False),
    (TimelineSnapshot, False),
    (AgentSpawned, True),
    (AgentUpdated, True),
    (NoticePosted, True),
    (NoticeResolved, True),
    (TaskCreated, True),
    (TaskUpdated, True),
    (ClusterUpdateStarted, True),
)


# TypeAdapter is faster than calling `ChatDelta.model_validate_json(...)`
# each time; one global instance is reused. Unknown roles raise
# `pydantic.ValidationError`. Derived from the registry so a new role is
# validated on the wire without a second hand-written list.
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(
    Annotated[
        Union[tuple(cls for cls, _ in _ROLE_CLASSES)],  # noqa: UP007 — runtime union construction, not an annotation
        Field(discriminator="role"),
    ]
)

# All events publish to settings.data_plane.events_channel (default 'ava:events');
# the UI subscribes to that one channel to receive every role. Derived from the
# registry — each class's `role` literal is the single declaration.
SYSTEM_ROLES: frozenset[str] = frozenset(
    cls.model_fields["role"].default for cls, _ in _ROLE_CLASSES
)

# Cross-agent, low-frequency subset forwarded on the global /api/system
# broadcast — every connected client sees these for *all* agents. Only the
# fleet-wide views consume them: the sidebar agent list (spawned / updated /
# label), the pages popover (page open / close), the FYI notice feed (notice
# posted / resolved), and cluster-update takeover. The high-frequency
# per-turn roles (chat_delta / code_delta / reasoning_delta /
# exec_output_chunk / timeline_snapshot / token_usage / ...) are deliberately
# excluded — they go only to the per-agent /api/agents/{id}/system stream of
# the agent currently being observed, so N open clients x M running agents do
# not fan every token slice out N*M-fold. GLOBAL_ROLES is a strict subset of
# SYSTEM_ROLES; the per-agent endpoint still forwards the full SYSTEM_ROLES
# (filtered by agent_id). The `True` flags in _ROLE_CLASSES are the derivation.
GLOBAL_ROLES: frozenset[str] = frozenset(
    cls.model_fields["role"].default for cls, g in _ROLE_CLASSES if g
)
