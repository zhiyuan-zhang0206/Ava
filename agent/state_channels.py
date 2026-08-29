"""Nested sub-state channel models for `BaseAgentState` — split out of
`agent/state.py` (issue #156) to keep that module under the 800-line ceiling.

Why these five live here and NOT in their own package: they are checkpoint
channel values. LangGraph's `JsonPlusSerializer` writes a pydantic-v2 channel
value as an ext object carrying its class's `(module, name)`, and
`shared/checkpoint_serde.py` allowlists the pairs that may deserialize. Moving
a model changes `__module__`, so **old checkpoints** (written when the class
lived at `agent.state.<Name>`) still resolve because `agent.state` re-exports
every name from here, and **new checkpoints** validate because
`agent.state_channels` is in the allowlist too. Both halves are load-bearing —
drop the re-export and old checkpoints stop deserializing; drop the new
allowlist entry and freshly-written checkpoints warn (and will block when
langgraph makes the allowlist strict). `tests/agent/test_checkpoint_serde.py`
locks both directions.
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, Field

from agent.nodes import CLAIM, NodeName


class CompactState(BaseModel):
    """Compaction bookkeeping, nested under `BaseAgentState.compact` (built-in
    since Issue #1284, grouped into a sub-state by the state-nesting refactor).

    `version` is a monotonic counter, +=1 on each successful compaction, never
    resets; subscribers (ava_code, ava_sdk_reminder) bookmark against it.
    `reminder_shown` / `reminder_seen_version` track the one-time wind-down
    reminder so it fires at most once per context window.

    The `compact` channel is last-value (no reducer): a writer reads the current
    value and `model_copy(update=...)`'s only the fields it changes, so bumping
    `version` alone (force / claim path) or flipping the reminder flags alone
    never resets the siblings.
    """

    version: int = 0
    reminder_shown: bool = False
    reminder_seen_version: int = 0


class AttachEntry(BaseModel):
    """One resolved local file awaiting delivery as a media message."""

    path: str
    label: str | None


def _new_pending_attachments() -> list[AttachEntry]:
    return []


class AttachState(BaseModel):
    """Files registered during a turn, drained into a media message by the exec node at the end of the registering turn (claim-boundary drain remains as the fallback for compact-halt edge paths)."""

    pending: list[AttachEntry] = Field(default_factory=_new_pending_attachments)


class MemoryState(BaseModel):
    """Passive-recall bookkeeping, nested under `BaseAgentState.memory`.

    `injected_paths` are memory-pool note paths already surfaced by passive
    recall this session, union-accumulated across turns (see
    `_memory_state_merge`) so the same note is not re-injected for the agent's
    life. Gated by turn_settings.agent.passive_memory_recall_enabled; stays
    empty when recall is off.
    """

    injected_paths: set[str] = Field(default_factory=set)


def _memory_state_merge(old: MemoryState, new: MemoryState) -> MemoryState:
    """Reducer for the `memory` channel: union the injected-path sets. The recall
    hook writes only the fresh paths each turn; this accumulates them across turns
    so a note recalled once stays deduped."""
    return MemoryState(injected_paths=old.injected_paths | new.injected_paths)


class CapabilitiesState(BaseModel):
    """What the `# Capabilities` index has actually told this agent about,
    nested under `BaseAgentState.capabilities`.

    `indexed` is the set of skill display identifiers the standing SystemMessage
    lists. It is recorded by `init_context` at the moment it builds that prompt
    and advanced by the drift check that keeps the listing honest between builds
    (`agent/hooks/capabilities.py`). The record has to exist because the two
    sides age differently: the index is rendered once per context window, while
    `ava.skills` under it is an uncached filesystem scan — without a snapshot,
    nothing in the loop can tell that they have diverged.

    `None` means no snapshot has been recorded for the current window — a
    checkpoint written before this field existed. The drift check then adopts the
    live catalog as its baseline in silence: what that agent's standing
    SystemMessage lists is unknowable from here, and announcing the whole catalog
    as newly installed would be a louder wrong answer than none.

    Last-value channel (no reducer): one writer per turn, and each writes the
    whole membership rather than a delta.
    """

    indexed: set[str] | None = None


class ContextReset(BaseModel):
    """A pending context (re)establishment, nested under
    `BaseAgentState.context_reset` — written by whoever clears the history,
    consumed and cleared by the `init_context` node.

    A compaction empties `messages` (RemoveMessage(REMOVE_ALL)) and hands the
    standing head back to `init_context` rather than re-assembling it: the node
    lays down the SystemMessage plus the ordered context notes, then `tail` —
    the post-compact summary alone (a compact is a clean wipe; chats that
    arrived while the turn was in flight are re-delivered as pending inbounds
    by the claim node instead of being parked here) — and routes on to
    `resume`.

    `resume` exists because a reset is requested from two depths: the forced
    compaction detours out of `before_llm` mid-turn, while the claim path has
    already computed where its batch should go next — which may be the graph's
    END, when a terminate rode in the same batch. Defaults to `claim`, the
    cold-start path, where nothing has been decided yet.

    Last-value channel (no reducer): one writer per reset, and `init_context`
    clears it by writing the default back.
    """

    tail: list[AnyMessage] = Field(default_factory=list)
    resume: NodeName = CLAIM


# Circuit-breaker reason vocabulary — the one field of `CircuitState.reason`.
# A permanent provider rejection opens the breaker with one of these; the
# overflow reason is the self-rescuable case (compaction), every other reason
# just stops heartbeat re-fires until a real wake succeeds.
CIRCUIT_REASON_CONTEXT_OVERFLOW = "context_overflow"
CIRCUIT_REASON_AUTH = "auth"
CIRCUIT_REASON_BILLING = "billing"
CIRCUIT_REASON_FORBIDDEN = "forbidden"
CIRCUIT_REASON_MODEL_NOT_FOUND = "model_not_found"
CIRCUIT_REASON_SCHEMA = "schema"
CIRCUIT_REASON_BAD_REQUEST = "bad_request"
CIRCUIT_REASON_PERMANENT = "permanent"


class CircuitState(BaseModel):
    """Heartbeat circuit breaker, nested under `BaseAgentState.circuit`.

    Opened by the runloop when a `FatalProviderError` (a PERMANENT provider
    rejection) aborts a turn: the next heartbeat would re-fire the same doomed
    LLM call (the 3962 incident — 80 heartbeat cycles against a permanent
    context-overflow 400, no self-rescue). While open:

    - a heartbeat check-in is consumed without appending its note or routing
      to the LLM (no doomed call, no context growth);
    - with `reason == CIRCUIT_REASON_CONTEXT_OVERFLOW`, any wake routes to a
      forced compaction instead — the overflow self-rescue (see
      `agent/hooks/compact.py:emergency_compact_summary` for the no-LLM
      fallback when the compact request itself is rejected).

    The breaker closes on the first successful LLM call (`llm_node`) — the
    provider accepting a request again is the healed signal. Last-value
    channel (no reducer), same discipline as `compact`: a writer reads the
    current value and `model_copy(update=...)`'s only the fields it changes.

    `reason` is a `CIRCUIT_REASON_*` value; `opened_at` is the ISO-8601 UTC
    timestamp of the rejection that opened it. Both are informational /
    queryable — the behaviour keys off `open` + `reason == ..._CONTEXT_OVERFLOW`.
    """

    open: bool = False
    reason: str | None = None
    opened_at: str | None = None

    @property
    def parks_idle(self) -> bool:
        """Whether an open breaker parks the agent idle instead of routing work
        to the LLM: open with any reason EXCEPT `context_overflow` — the
        overflow reason routes to the forced-compact self-rescue instead, so
        it must keep flowing to decide()'s compact arm."""
        return self.open and self.reason != CIRCUIT_REASON_CONTEXT_OVERFLOW


__all__ = [
    "CIRCUIT_REASON_AUTH",
    "CIRCUIT_REASON_BAD_REQUEST",
    "CIRCUIT_REASON_BILLING",
    "CIRCUIT_REASON_CONTEXT_OVERFLOW",
    "CIRCUIT_REASON_FORBIDDEN",
    "CIRCUIT_REASON_MODEL_NOT_FOUND",
    "CIRCUIT_REASON_PERMANENT",
    "CIRCUIT_REASON_SCHEMA",
    "AttachEntry",
    "AttachState",
    "CapabilitiesState",
    "CircuitState",
    "CompactState",
    "ContextReset",
    "MemoryState",
]
