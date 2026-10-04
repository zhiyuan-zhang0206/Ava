"""LangGraph state — supports whole-class plugin state registration.

BaseAgentState (framework layer, static):
    messages         — cross-turn LLM history (guarded add_messages reducer: append-only invariant, task #1256)
    halted           — whether the current turn has ended
    turn_active      — the current graph invocation is mid-turn (claim's turn boundary)
    exit_requested   — claim's END means process exit, not just turn over
    turn_idle        — hosted mode: claim found nothing and did not park, so the host ends the turn task
    update_initiated — this agent kicked off a cluster self-update
    compact          — nested compaction bookkeeping (CompactState, agent/state_channels.py)
    circuit          — nested heartbeat circuit-breaker state (CircuitState, agent/state_channels.py)
    attach           — nested next-turn attachment queue (AttachState, agent/state_channels.py)
    memory           — nested passive-recall bookkeeping (MemoryState, agent/state_channels.py)
    capabilities     — nested capability-index snapshot (CapabilitiesState, agent/state_channels.py)
    context_reset    — nested pending-context-reset bookkeeping (ContextReset, agent/state_channels.py)

A plugin declares an entire BaseModel in its `contribute()` (`PluginContributions.state`) and
keeps a `PluginStateHandle[Cls]` (built from the class and its plugin name) for typed read/write;
framework dispatches by field name:

- Field name ∈ BaseAgentState (messages / halted): treated as "plugin
  declares it will modify this base field"; no prefix added; type must
  match base exactly (including Annotated reducer), otherwise raise.
  Multiple plugins declaring the same base field all modify the same
  channel; reducer naturally merges.
- Field name ∉ base: auto-prefixed `<plugin>__<field>` into the merged
  AgentState; plugin-private channel.

`build_agent_state(extensions)` dynamically creates AgentState (BaseAgentState
subclass + plugin-declared fields) at graph build time. A plugin reaches its state two
ways, one per side of the exec process boundary, both through its `PluginStateHandle`:

- host side (graph hooks, in the agent process): `handle.view(state)` builds the typed
  snapshot from the graph `state` argument, and `handle.delta({...})` turns a plugin-local
  update into the prefixed update dict the hook returns for LangGraph's reducer. Pure
  functions of the class and the state — no SDK slot, no `ava`.
- exec side (SDK functions running in the exec child): `handle.read()` / `handle.update()`
  work on the exec turn's slot (`ava.state` / `ava.state_update`, framework-internal).

Usage (in a plugin's agent_runtime.py):

    from agent.state import PluginStateHandle
    from base.packages.plugins.extensions import PluginContributions
    from pydantic import BaseModel, Field
    from typing import Annotated

    def _set_union(old: set[str], new: set[str]) -> set[str]:
        return old | new

    class MyPluginState(BaseModel):
        counter: int = Field(default=0)
        seen: Annotated[set[str], _set_union] = Field(default_factory=set)

    state_handle = PluginStateHandle(MyPluginState, "my_plugin")
    # exec side: state_handle.read() -> MyPluginState; state_handle.update({"counter": 1})
    # host side, in a hook(state, runtime, config): state_handle.view(state) -> MyPluginState,
    #   return state_handle.delta({"counter": 1})  # -> {"my_plugin__counter": 1}

    def contribute() -> PluginContributions:
        return PluginContributions(state=(MyPluginState,))
"""

import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from agent.messages.guard import guarded_add_messages, guarded_delta_reducer

# The five nested sub-state channel models live in agent/state_channels
# (issue #156 — this module sat at the 800-line ceiling). They are RE-EXPORTED
# here, never re-defined: LangGraph checkpoints written before the split carry
# ("agent.state", "<Name>") ext envelopes, and the serializer resolves them by
# module attribute lookup — so old checkpoints keep deserializing only while
# these names stay importable from agent.state. New checkpoints carry
# ("agent.state_channels", "<Name>") and are allowlisted in
# base/agents/history/checkpoint_serde.py alongside the legacy pairs.
from agent.state_channels import AttachEntry as _AttachEntry
from agent.state_channels import (
    AttachState,
    CapabilitiesState,
    CircuitState,
    CompactState,
    ContextReset,
    MemoryState,
    _memory_state_merge,
)
from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.agents.messages.security_finding import SecurityFindingEntry
from base.packages.plugins.extensions import (
    PLUGIN_WRITABLE_BASE_FIELDS,
    SDK_WRITTEN_BASE_FIELDS,
    ExtensionRegistry,
)

AttachEntry = _AttachEntry

# The messages channel's delta form and snapshot cadence (write switch,
# task #3180 — see BaseAgentState.messages).
_MESSAGES_DELTA_SNAPSHOT_FREQUENCY = 1000
_MESSAGES_DELTA_CHANNEL = DeltaChannel(
    guarded_delta_reducer, snapshot_frequency=_MESSAGES_DELTA_SNAPSHOT_FREQUENCY
)


class BaseAgentState(BaseModel):
    """Framework static state — base fields all agents have.

    Flat fields (messages / halted / update_initiated) sit at the top level;
    compaction, attachments, passive-recall, and pending-context-reset
    bookkeeping are grouped into nested sub-states, each its own LangGraph
    channel holding a BaseModel.
    """

    # The messages channel is a LangGraph DeltaChannel since the 2026-09-14
    # write switch (task #3180): each super-step appends its messages delta
    # to `checkpoint_writes` (periodic `_DeltaSnapshot` blobs at
    # _MESSAGES_DELTA_SNAPSHOT_FREQUENCY); readers fold at read time, and the
    # read-compat layer keeps pre-switch threads readable through rollback
    # (base/agents/history/delta_read_compat.py). The reducer is the delta form of the
    # append-only guard — guarded_delta_reducer replays stored writes through
    # guarded_add_messages, so the invariant (user ruling 2026-08-13, task
    # #1256 — only a full wipe, a tail append, or modifying the last message
    # is allowed; see agent/messages/guard.py) holds on every replay. The
    # single-merge form (guarded_add_messages) stays the working-copy /
    # plugin-update merge.
    messages: Annotated[list[AnyMessage], _MESSAGES_DELTA_CHANNEL] = Field(default_factory=list)
    pending_exec_notes: list[AnyMessage] = Field(default_factory=list)
    """Notes/media deferred until all tool results; recovery drains after repair."""
    security_findings: Annotated[list[SecurityFindingEntry], operator.add] = Field(
        default_factory=list
    )
    """Prompt-injection findings the exec child raised, awaiting delivery: `ava.security` appends
    through `ava.state_update`; the after_exec hook (`agent/hooks/security.py`) turns them into
    SECURITY notes and resets the channel with `Overwrite([])`."""
    halted: bool = False
    turn_active: bool = False
    """This invocation is mid-turn (claim routed work). One invocation = one
    turn: a claim pass that finds nothing to do with this set ends the
    invocation (goto END) instead of blocking, so the runloop can close the
    turn's root span and re-invoke."""
    exit_requested: bool = False
    """Claim accepted termination; the host flushes and applies it at END."""
    restart_requested: bool = False
    """Claim accepted restart; the host flushes, applies it, and releases the runtime."""
    turn_idle: bool = False
    """Claim found no work; the host ends the turn task. Reset per invocation."""
    update_initiated: bool = False
    """Set by self-initiated restarts (`ava.self.restart()`); the historical
    `ava.self.update()` initiator path that introduced it is removed, but
    self-sourced restarts still set it (claim's restart handler), and it feeds
    the idle-restart gate — a stale True is cleared by the system:update
    restart_completed marker (2026-08-08 audit, P3-6: comment said the field
    was dead, it is not)."""
    active_task_id: int | None = None
    """Explicit task driving the current turn's LLM usage, if any.

    Claim sets this only from a task-associated system note and clears it for
    chat or unassociated inbound work, so ownership never implies attribution.
    """

    impersonation_handoff_id: str | None = None
    impersonation_request_id: str | None = None
    """Last presented takeover request; survives history compaction."""
    impersonation_applied: dict[str, object] = Field(default_factory=dict)
    """Atomic checkpoint receipt for the last external plugin delta applied."""

    compact: CompactState = Field(default_factory=CompactState)
    """Compaction bookkeeping — nested last-value channel (see CompactState)."""

    circuit: CircuitState = Field(default_factory=CircuitState)
    """Heartbeat circuit breaker — nested last-value channel (see CircuitState)."""

    attach: AttachState = Field(default_factory=AttachState)
    """Pending files — nested last-value channel (see AttachState)."""

    memory: Annotated[MemoryState, _memory_state_merge] = Field(default_factory=MemoryState)
    """Passive-recall bookkeeping — nested union-reducer channel (see MemoryState)."""

    context_reset: ContextReset = Field(default_factory=ContextReset)
    """Pending context (re)establishment — nested last-value channel (see ContextReset)."""

    capabilities: CapabilitiesState = Field(default_factory=CapabilitiesState)
    """What the `# Capabilities` index has surfaced — nested last-value channel
    (see CapabilitiesState)."""


# ── BaseAgentState field snapshot (for plugin_state_schema field-name dispatch) ──

_BASE_FIELDS: frozenset[str] = frozenset(BaseAgentState.model_fields.keys())


# What a registry's plugin state declarations amount to, built by `plugin_state_schema` and carried by
# the AgentState class `build_agent_state` makes from it:
#   extra_fields: prefixed key → (annotation, FieldInfo), stuffed into the class namespace so
#       LangGraph and Pydantic see all plugin fields
#   namespace_fields: plugin_name → set[original field name (no prefix)], so AgentState.__getattr__
#       knows which prefixed fields belong to which plugin when returning `state.<plugin>`
#       (cross-plugin isolation)
#   base_declared: core fields some plugin declared in its BaseModel (only `messages`): "plugin
#       explicitly updates the base channel" is legal, a base-field write without a declaration is
#       a missing-prefix typo and `_validate_plugin_state_keys` rejects it
#   classes: the declared BaseModel classes — the msgpack allowlist must cover them
_StateFieldSpec = tuple[Any, FieldInfo]


@dataclass(frozen=True)
class PluginStateSchema:
    extra_fields: dict[str, _StateFieldSpec]
    namespace_fields: dict[str, set[str]]
    base_declared: frozenset[str]
    classes: frozenset[type[BaseModel]]


# Core keys a plugin may declare/write: only `messages` (its add_messages reducer defines the
# merge contract; exec._notes.merge_exec_notes combines a plugin's messages delta with the exec
# ToolMessage — tool result first, notes after, per the Anthropic-compat adjacency constraint).
# Every other BaseAgentState field is framework-managed per turn: declaring one is rejected by
# `plugin_state_schema`, and a direct ava.state_update write to one is rejected by
# _validate_plugin_state_keys (except `SDK_WRITTEN_BASE_FIELDS`, which the SDK itself writes).
_PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = PLUGIN_WRITABLE_BASE_FIELDS

# BaseAgentState built-in fields. `messages` is the one a plugin may modify (only when it declares
# it in its own BaseModel with the exact BaseAgentState annotation; plugin_state_schema checks, and
# PluginStateSchema.base_declared tracks the declared set). A direct write to any other base channel
# = plugin missing a prefix typo (writing "compact" instead of "ava_myplugin__compact"), which would
# silent-clobber this turn's ToolMessage / lifecycle signal / compaction state; must blow up.
# Derived from BaseAgentState.model_fields (via state._BASE_FIELDS) so the guard tracks the base as
# it grows/shrinks — no hardcoded list to drift (I-8).
_BASE_STATE_FIELDS: frozenset[str] = _BASE_FIELDS


def _validate_plugin_state_keys(update: dict[str, Any], state_cls: type[Any]) -> dict[str, Any]:
    """fail-fast: plugin writing to ava.state_update with illegal keys must raise.

    Two classes of abuse raise — AGENTS.md "fail-fast / no silent fallback":
    1. Base field written but the plugin did not explicitly declare it in
       BaseModel → missing prefix typo
    2. Key not in state schema → LangGraph reducer silently drops outside
       schema; plugin author typos have no diagnostic pointer

    Explicitly declared base fields (the class's `__plugin_base_declared__`) are allowed:
    plugin writing via PluginStateHandle.update({"messages": [...]}) to the
    base channel is a legitimate path.

    Runs before exec_node returns, so plugin errors blow up that turn with traceback.
    """
    if not update:
        return update
    base_clash = set(update) & _BASE_STATE_FIELDS
    base_declared: frozenset[str] = getattr(state_cls, "__plugin_base_declared__", frozenset())
    illegal_base = base_clash - base_declared - SDK_WRITTEN_BASE_FIELDS
    if illegal_base:
        raise ValueError(
            f"plugin wrote undeclared base field to ava.state_update: {sorted(illegal_base)} — "
            f"framework core keys are managed by the framework every turn; only "
            f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable, and only when declared "
            f"in the plugin's own BaseModel with the exact BaseAgentState annotation. "
            f"Missing prefix typo? Declared: {sorted(base_declared) or '<empty>'}"
        )
    # Legal keys = all field names in the state schema (including base — already validated through illegal_base).
    # Previously used `model_fields - _BASE_STATE_FIELDS` to exclude base; now allowing declared
    # base fields to be written, no longer excluded.
    known = set(state_cls.model_fields.keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    unknown = set(update) - known
    if unknown:
        raise ValueError(
            f"plugin wrote unregistered key to ava.state_update: {sorted(unknown)} — "
            f"fields not declared in a plugin's `state` contribution are silently dropped by the "
            f"LangGraph reducer in Command(update=). Known plugin fields: "
            f"{sorted(known) or '<empty>'}"
        )
    return update


# LangGraph style: `Annotated[T, reducer_fn]` stuffs reducer into Pydantic
# FieldInfo.metadata, same as the `messages` channel annotation on
# BaseAgentState (its delta form since the write switch, task #3180). When
# plugin fields don't declare a reducer, default is last-value (overwrite) —
# same semantics as LangGraph LastValue channel.


# Canonical sentinel for the messages-channel reducer in annotation
# comparisons (see _messages_annotation_key).
_MESSAGES_REDUCER = object()


def _is_messages_reducer_form(m: Any) -> bool:
    """Every spelling of the messages-channel reducer contract: plain
    `add_messages` (what plugins declare), `guarded_add_messages` (the
    pre-switch channel reducer), and the delta form the channel runs since
    the 2026-09-14 write switch (`DeltaChannel(guarded_delta_reducer, ...)`,
    task #3180). All three are the same contract."""
    if m is add_messages or m is guarded_add_messages:
        return True
    return isinstance(m, DeltaChannel) and m.reducer is guarded_delta_reducer


def _messages_annotation_key(annotation: Any) -> tuple[Any, ...]:
    """Canonical key for comparing base-field annotations: the messages
    reducer may be spelled `add_messages` (the contract plugins declare in
    their own BaseModel), `guarded_add_messages` (the pre-switch channel
    reducer, task #1256 — add_messages plus the append-only invariant), or
    the channel's delta form (`DeltaChannel(guarded_delta_reducer, ...)`,
    the write switch, task #3180); all are the same contract. Anything else
    compares as-is and differs."""
    meta = getattr(annotation, "__metadata__", None)
    if meta:
        origin = getattr(annotation, "__origin__", None)
        return (
            origin,
            *(_MESSAGES_REDUCER if _is_messages_reducer_form(m) else m for m in meta),
        )
    return (annotation,)


def _resolve_reducer(field: FieldInfo) -> Callable[[Any, Any], Any]:
    """Extract the LangGraph reducer function from Pydantic FieldInfo.metadata; returns last-value if none.

    LangGraph Annotated[T, reducer] stuffs reducer as metadata into BaseModel
    fields; Pydantic v2 collects metadata into FieldInfo.metadata list.
    Iterate finding the first callable (excluding type itself, to avoid
    Annotated[T, int]-style false-positives treating int as reducer).
    """
    for m in field.metadata:
        if isinstance(m, DeltaChannel):
            # The messages channel's delta form (write switch, task #3180).
            # The single-merge used by the working copy / plugin updates /
            # external deltas stays the guarded merge (`guarded_add_messages`)
            # — one delta = one guarded merge, which is what the channel-side
            # replay (guarded_delta_reducer) applies per stored write. Never
            # the last-value fallback: that would silently overwrite the
            # message list.
            if m.reducer is guarded_delta_reducer:
                return guarded_add_messages
            continue
        if callable(m) and not isinstance(m, type):
            # A plugin declaring the base `messages` field spells the contract
            # annotation `add_messages`; the channel's reducer is the guarded
            # form (same semantics + the append-only invariant, task #1256).
            # Route the working-copy merge through the guard too, so an
            # in-turn plugin violation fails inside execute_code instead of
            # only at commit.
            return guarded_add_messages if m is add_messages else m
    return lambda _old, new: new  # last-value (overwrite)


def _accumulate_delta(acc: Any, new: Any, reducer: Callable[[Any, Any], Any]) -> Any:
    """Merge a fresh delta into the turn's accumulated delta for one channel.

    The accumulated value is what the LangGraph reducer sees at commit
    (`reducer(checkpoint_value, accumulated)`), so it must be the batch merge
    that reproduces sequential application — for deltas d1..dn:

        reducer(checkpoint, merge(d1..dn)) ==
            reducer(...reducer(reducer(checkpoint, d1), d2)..., dn)

    For monoid reducers (set-union, last-value overwrite), `reducer(acc, new)`
    IS that merge. LangGraph's `add_messages` is not a monoid over deltas:
    RemoveMessage markers and REMOVE_ALL only have meaning against the full
    message list, so `add_messages(acc, new)` raises on a removal whose id
    exists in the checkpoint but not in `acc`, and a REMOVE_ALL applied to
    `acc` wipes the accumulated delta itself (commit then sees 'no update').
    Its correct batch merge is plain concatenation — the batching-invariance
    LangGraph's own DeltaChannel requires (`reducer(reducer(state, xs), ys) ==
    reducer(state, xs + ys)`); the commit side processes the concatenated
    list in order and produces exactly the working copy.
    """
    if _is_messages_reducer_form(reducer):
        acc_list = acc if isinstance(acc, list) else [acc]
        new_list = new if isinstance(new, list) else [new]
        return acc_list + new_list
    return reducer(acc, new)


class PluginStateHandle[T: BaseModel]:
    """Typed read/write handle for plugin state. Built from a declared state class and its plugin's name.

    Two sides, because the plugin's state is touched from two processes:

    Host side — a graph hook runs in the agent process and was handed the graph `state`;
    it never touches the SDK's exec slot (which does not exist there):

        handle.view(state) -> T                     (typed snapshot of the graph state)
        handle.delta({"field": value}) -> dict      (the update dict the hook returns)

    Both are pure; LangGraph's reducer applies the returned dict.

    Exec side — SDK functions running inside the exec child (`ava.cwd.set`, the read wrap)
    work on the turn's slot. API shape matches LangGraph Command(update=dict):

        handle.read() -> T                          (typed snapshot)
        handle.update({"field": delta_value})       (validated + reducer-merged)

    Within the same turn, update() is immediately visible: handle
    simultaneously mutates the `ava.state` working copy + accumulates the
    raw delta into `ava.state_update`. At turn end, exec_node merges
    state_update into Command(update=...), and the LangGraph reducer runs
    again on the commit side (with the accumulated delta as reducer input,
    producing the same result as the working copy — reducers must be
    batching-invariant, i.e. merging deltas then applying once must equal
    applying them in sequence; `_accumulate_delta` implements the merge).
    Outside an exec turn `read()` / `update()` raise `PluginStateOutsideTurnError`.

    Base field reuse: if a plugin BaseModel declares a field with the same
    name as one in BaseAgentState (types matching exactly, validated at
    plugin_state_schema), the handle routes those fields to the
    base channel (no prefix). A single update dict can mix base / plugin
    fields; the handle internally dispatches by _BASE_FIELDS.
    """

    def __init__(self, cls: type[T], plugin_name: str | None) -> None:
        self._cls = cls
        self._plugin_name = plugin_name
        # Field name → actual LangGraph channel key.
        #   base field: bare name, shares BaseAgentState channel.
        #   plugin field: <plugin>__<field>; when plugin_name=None, falls back
        #   to bare name (supports test fixtures directly calling PluginStateHandle
        #   — normal declared-state path always passes name).
        self._channel_keys: dict[str, str] = {}
        for name in cls.model_fields:
            if name in _BASE_FIELDS:
                self._channel_keys[name] = name
            else:
                self._channel_keys[name] = f"{plugin_name}__{name}" if plugin_name else name
        # Reducer per field. Base fields must declare exactly as BaseAgentState
        # (including Annotated reducer; plugin_state_schema already
        # validates), so extracting what the plugin wrote gives the same
        # reducer as base; no need to look up base separately.
        self._reducers: dict[str, Callable[[Any, Any], Any]] = {
            name: _resolve_reducer(field) for name, field in cls.model_fields.items()
        }

    def view(self, state: object) -> T:
        """Typed snapshot of this plugin's fields in a graph `state` — a hook's argument.

        The graph already validated `state`, so the fields are taken as they stand
        (`model_construct`): no second validation, no copy of the message history a plugin
        may declare. Pure: reads only `state`.
        """
        return self._cls.model_construct(
            **{f: getattr(state, self._channel_keys[f]) for f in self._cls.model_fields}
        )

    def delta(self, update: Mapping[str, Any]) -> dict[str, Any]:
        """The graph-state update dict for plugin-local `update` — what a hook returns.

        Keys are plugin-local field names (no prefix); the result is keyed by the actual
        channels, ready for LangGraph's reducer. Pure.

        Raises:
            ValueError: `update` has a key outside the BaseModel schema (plugin author typo).
        """
        out: dict[str, Any] = {}
        for field, value in update.items():
            if field not in self._cls.model_fields:
                raise ValueError(
                    f"PluginStateHandle[{self._cls.__name__}].delta: unknown field "
                    f"{field!r} (schema declares {sorted(self._cls.model_fields)})"
                )
            out[self._channel_keys[field]] = value
        return out

    def read(self) -> T:
        """Current exec-turn snapshot. Reflects all `update()` calls in same turn.

        Raises:
            PluginStateOutsideTurnError: not inside an exec turn (`ava.state` is unbound).
        """
        import ava  # lazy import: avoid circular (ava imports agent.state via plugin loading)
        from ava.agent_identity import validate_external_identity

        validate_external_identity()

        slot = ava.state
        return self._cls.model_validate(
            {f: getattr(slot, self._channel_keys[f]) for f in self._cls.model_fields}
        )

    def update(self, delta: dict[str, Any]) -> None:
        """Apply field updates inside an exec turn. Same API shape as LangGraph Command(update=dict).

        Each field's reducer merges `current ⊕ delta`; the result is written
        to the `ava.state` working copy (immediately visible to `read()`
        within this turn) + raw delta accumulated into `ava.state_update`
        (at turn end, LangGraph commits and the reducer runs again to get
        the final value).

        Keys use plugin-local field names (no prefix). The handle internally
        dispatches base/plugin to the correct channel.

        Raises:
            PluginStateOutsideTurnError: not inside an exec turn (`ava.state` is unbound).
            TypeError: the exec code replaced `ava.state_update` with a non-dict.
            ValueError: delta contains a key outside the BaseModel schema (plugin author typo).
        """
        import ava
        from ava.agent_identity import validate_external_identity

        validate_external_identity()

        slot = ava.state
        accumulated = ava.state_update
        if not isinstance(accumulated, dict):
            raise TypeError(
                f"ava.state_update must stay a dict, got {type(accumulated).__name__} "
                f"(PluginStateHandle[{self._cls.__name__}].update)"
            )
        for field, new in delta.items():
            if field not in self._cls.model_fields:
                raise ValueError(
                    f"PluginStateHandle[{self._cls.__name__}].update: unknown field "
                    f"{field!r} (schema declares {sorted(self._cls.model_fields)})"
                )
            channel_key = self._channel_keys[field]
            current = getattr(slot, channel_key)
            merged = self._reducers[field](current, new)
            setattr(slot, channel_key, merged)  # working copy synchronously visible
            # Accumulate the raw delta into ava.state_update. The previous
            # raw-overwrite dropped every earlier delta to a reducer field in
            # one turn: two `update({"seen": {"a"}})` + `update({"seen":
            # {"b"}})` committed only {"b"}, while read() inside the turn saw
            # both — the classic silent-loss shape (2026-08-08 audit,
            # cc-backend-runtime P1). For last-value fields the reducer is
            # overwrite, so the accumulation collapses to the latest delta
            # exactly as before (see `_accumulate_delta` for the merge rule).
            if channel_key in accumulated:
                accumulated[channel_key] = _accumulate_delta(
                    accumulated[channel_key], new, self._reducers[field]
                )
            else:
                accumulated[channel_key] = new


def compact_version() -> int:
    """The exec turn snapshot's `compact.version` — the built-in compaction counter.

    A built-in channel no plugin declares, so it is not reachable through a
    `PluginStateHandle`; exec-side code that resets per-compaction bookkeeping (injection
    dedup) reads it here instead of reaching into `ava.state`.

    Raises:
        PluginStateOutsideTurnError: not inside an exec turn (`ava.state` is unbound).
    """
    import ava
    from ava.agent_identity import validate_external_identity

    validate_external_identity()

    return ava.state.compact.version


def _annotation_text(annotation: Any) -> str:
    """`str` / `set[str]` / `str | None` — an annotation spelled the way its
    author wrote it, for the contribution ledger's one-line detail (a bare class
    reprs as `<class 'str'>`, which is noise in a catalog)."""
    return annotation.__name__ if isinstance(annotation, type) else repr(annotation)


def plugin_state_schema(extensions: ExtensionRegistry) -> PluginStateSchema:
    """The state declarations of every plugin in `extensions`, validated.

    A plugin's `PluginContributions.state` classes dispatch by field name:

    - Field name ∈ BaseAgentState → no prefix, shares the same channel
      with base. Only `messages` is plugin-writable (`PLUGIN_WRITABLE_BASE_FIELDS`);
      declaring any other core key (halted / update_initiated / compact /
      memory / context_reset / capabilities) raises — those are
      framework-managed every turn. For `messages`, type must match base
      exactly (including Annotated reducer — the messages reducer may be
      spelled either `add_messages` or the guarded wrapper, same contract),
      otherwise raise — "plugin declares it will modify this base field"
      contract; not allowed to silently change types. The declaration is recorded in
      `base_declared` so the framework's state_update key validation lets it through, and
      the exec node merges the plugin's messages delta with its own ToolMessage delta.
    - Field name ∉ base → auto-prefixed `<plugin>__<field>` into the
      dynamic AgentState; plugin-private channel.

    Raises:
        TypeError: a declared class is not a BaseModel subclass — plugin author misused dataclass / plain class.
        ValueError: field name ∈ base but annotation doesn't match base (type swap), or a
            plugin declares one prefixed field twice with conflicting types.
    """
    extra_fields: dict[str, _StateFieldSpec] = {}
    namespace_fields: dict[str, set[str]] = {}
    base_declared: set[str] = set()
    classes: set[type[BaseModel]] = set()
    for plugin, cls in extensions.state_classes():
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            raise TypeError(
                f"plugin {plugin!r} declared state {cls!r}, which is not a BaseModel subclass — "
                f"write `class FooState(BaseModel): ...` and declare it in `PluginContributions.state`."
            )
        classes.add(cls)
        # An empty-field class must still record the plugin, so `state.<plugin>` returns an empty
        # SimpleNamespace rather than AttributeError ("declared a class but only writes base
        # fields" gets a consistent view too).
        namespace_fields.setdefault(plugin, set())
        for name, model_field in cls.model_fields.items():
            # Pydantic v2 splits `Annotated[T, ...metadata]` into `model_field.annotation` (bare T)
            # + `model_field.metadata` (Annotated's extras list); reconstruct the full Annotated to
            # compare with the original on BaseAgentState.model_fields (the messages channel may be
            # spelled by its delta form or the guarded reducer; the comparison normalizes the
            # contract spellings — see _messages_annotation_key). Don't read
            # `cls.__annotations__`: under `from __future__ import annotations` those are strings,
            # while Pydantic already evaluated them into model_field.
            if model_field.metadata:
                raw_annotation = Annotated[model_field.annotation, *model_field.metadata]
            else:
                raw_annotation = model_field.annotation

            if name in _BASE_FIELDS:
                if name not in _PLUGIN_WRITABLE_BASE_FIELDS:
                    raise ValueError(
                        f"plugin {plugin!r} declared core state field {name!r} — only "
                        f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable among the "
                        f"framework core keys; {name!r} is framework-managed every turn "
                        f"(write a private <plugin>__{name} field or contribute via a hook instead)"
                    )
                base_raw = BaseAgentState.__annotations__[name]
                if _messages_annotation_key(raw_annotation) != _messages_annotation_key(base_raw):
                    raise ValueError(
                        f"plugin {plugin!r} declared base field '{name}' with type "
                        f"{raw_annotation!r} differing from BaseAgentState's {base_raw!r} — "
                        f"declaring a base field is a contract 'plugin will modify this field'; "
                        f"silent type swaps not allowed."
                    )
                # Base field shared: BaseAgentState already has the full definition (including
                # reducer); build_agent_state won't rewrite it.
                base_declared.add(name)
                continue

            prefixed = f"{plugin}__{name}"
            if prefixed in extra_fields:
                existing_annotation, _ = extra_fields[prefixed]
                if existing_annotation != raw_annotation:
                    raise ValueError(
                        f"state field {prefixed!r} type conflict: "
                        f"{existing_annotation!r} vs {raw_annotation!r}"
                    )
            else:
                extra_fields[prefixed] = (raw_annotation, model_field)
            namespace_fields[plugin].add(name)
    return PluginStateSchema(
        extra_fields, namespace_fields, frozenset(base_declared), frozenset(classes)
    )


def _plugin_namespace_view(state: BaseAgentState, plugin: str) -> SimpleNamespace:
    """Construct the SimpleNamespace view for `state.<plugin>` — auto-strips
    `<plugin>__` prefix.

    `__getattr__` entry point uses this; extracted as a helper to let tests
    stub directly.
    """
    namespaces: dict[str, set[str]] = getattr(type(state), "__plugin_namespace_fields__", {})
    fields = namespaces.get(plugin)
    if fields is None:
        # Plugin declares no state → list known plugin names so typos
        # are immediately visible. Base fields (messages/halted) don't
        # belong to any plugin; read directly via ava.state.messages.
        known = sorted(namespaces)
        raise AttributeError(
            f"ava.state.{plugin} does not exist — plugin {plugin!r} declares no state, "
            f"or plugin name typo. Known plugins: {known or '<empty>'}"
        )
    prefix = f"{plugin}__"
    return SimpleNamespace(**{name: getattr(state, f"{prefix}{name}") for name in fields})


def build_agent_state(extensions: ExtensionRegistry) -> type[BaseAgentState]:
    """Build dynamic AgentState: BaseAgentState subclass + all plugin-declared fields.

    Uses subclass (rather than `pydantic.create_model`) to guarantee
    BaseAgentState's field (messages/halted) FieldInfo objects are exactly
    the same — LangGraph channel type comparison relies on FieldInfo equality
    (__eq__); create_model would rebuild FieldInfo and != would treat them
    as different types.

    The new class's `__module__` is set to this module so Pydantic resolves its forward references
    there. Nothing is bound into the module: the graph runs on the class this returns
    (`build_graph` hands it to every node as its `input_schema`), and a consumer outside the graph
    takes the class it needs from its own caller.

    The AgentState class has `__getattr__`: `state.<plugin_name>` returns a
    SimpleNamespace view (auto-strips `<plugin>__` prefix). Lets agents
    write `ava.state.ava_code.cwd` rather than `ava.state.ava_code__cwd`;
    plugin-private namespaces also isolated cross-plugin.
    """
    schema = plugin_state_schema(extensions)
    if not schema.extra_fields and not schema.namespace_fields:
        # Without plugin fields, also can't directly return BaseAgentState
        # — it has no plugin namespace __getattr__; but actually "no
        # plugin" also means no one will use state.<plugin>, so returning
        # BaseAgentState is simpler and reuses the LangGraph channel old
        # implementation path.
        return BaseAgentState

    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {
        "__annotations__": annotations,
        # The class carries its own schema: the namespace view and the state-key validation read it
        # off the class, so no module-level table can drift from the class a graph runs on.
        "__plugin_namespace_fields__": schema.namespace_fields,
        "__plugin_base_declared__": schema.base_declared,
        "__plugin_state_classes__": schema.classes,
    }
    for name, (annotation, field) in schema.extra_fields.items():
        annotations[name] = annotation
        namespace[name] = field

    def _state_getattr(self: BaseAgentState, item: str) -> SimpleNamespace:
        # Pydantic BaseModel.__getattr__ doesn't exist (it goes through
        # __getattribute__ to directly fetch fields), so this __getattr__
        # is only called after "ordinary attribute lookup failed" — won't
        # contend paths with base fields / prefixed plugin fields. Dunders
        # + underscore-prefix are universally not taken over (Pydantic /
        # Python internal use); let Python take the default AttributeError
        # path; otherwise go through _plugin_namespace_view, which raises
        # AttributeError with a "known plugin names" list on unregistered
        # plugin names, making `state.<typo>` typos immediately visible
        # rather than silently returning an empty namespace.
        # Note: _plugin_namespace_view reads the schema off the instance's class, so an instance
        # always sees the plugin set its own class was built from.
        if item.startswith("_"):
            raise AttributeError(item)
        return _plugin_namespace_view(self, item)

    namespace["__getattr__"] = _state_getattr

    cls = type("AgentState", (BaseAgentState,), namespace)
    cls.__module__ = __name__
    return cls


# Static name for annotations and tests: the base schema. The class a graph runs on, with the
# plugins' fields, is what `build_agent_state(registry)` returns.
AgentState = BaseAgentState


# ── Checkpoint msgpack allowlist ──


def checkpoint_msgpack_allowlist(
    plugin_state_classes: Iterable[type[BaseModel]] = (),
) -> frozenset[tuple[str, str]]:
    """LangGraph checkpoint msgpack allowlist — `(module, name)` pairs the
    framework's checkpoint serde may deserialize.

    Every nested sub-state channel value (`compact` / `attach` / `memory` /
    `context_reset` / `capabilities`) is a Pydantic v2 model, and
    `JsonPlusSerializer` serializes such values as an ext object carrying the
    class's `(module, name)`; without an explicit registration the serializer
    runs in its permissive mode and warns on **every** checkpoint load ("This
    will be blocked in a future version"). Registration is per class — the
    schema being stable and importable is not enough, the type must be named
    in the allowlist.

    The dynamic `AgentState` subclass is listed by the name it carries ("AgentState" in this
    module), which the module-level alias shares, so the entry is correct regardless of build
    order. Plugin classes
    declared in `PluginContributions.state` are included automatically — a
    plugin field holding a BaseModel instance crosses the checkpointer as that
    class and would otherwise be blocked (degraded to a plain dict) the moment
    the allowlist replaces the permissive default.

    Consumers: `services/agent_runner/agent_host/daemon.py::_build_checkpointer` (shared host saver) and
    any embedding checkpoint saver.
    """
    entries: set[tuple[str, str]] = set(STATIC_CHECKPOINT_MSGPACK_TYPES)
    for cls in plugin_state_classes:
        entries.add((cls.__module__, cls.__name__))
    return frozenset(entries)


def process_state_classes() -> frozenset[type[BaseModel]]:
    """The plugin state classes the `agent_runtime` faces loaded into this process declare, for a
    serializer that runs in a process holding no registry of its own (the exec IPC, an external
    attachment). Read off the loaded faces, so none before they load."""
    from agent.extensions.registry import loaded_state_classes

    return loaded_state_classes()


def build_checkpoint_serde(
    plugin_state_classes: Iterable[type[BaseModel]] = (),
) -> JsonPlusSerializer:
    """JsonPlusSerializer with the framework checkpoint allowlist — pass as
    `serde=` when constructing the hosted LangGraph checkpointer."""
    return JsonPlusSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(plugin_state_classes)
    )
