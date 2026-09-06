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

Plugins register an entire BaseModel via `register_plugin_state(Cls)`,
getting back a `PluginStateHandle[Cls]` for typed read/write; framework
dispatches by field name:

- Field name ∈ BaseAgentState (messages / halted): treated as "plugin
  declares it will modify this base field"; no prefix added; type must
  match base exactly (including Annotated reducer), otherwise raise.
  Multiple plugins declaring the same base field all modify the same
  channel; reducer naturally merges.
- Field name ∉ base: auto-prefixed `<plugin>__<field>` into the merged
  AgentState; plugin-private channel; two plugins with the same name and
  same type → fail-fast raise forcing rename.

`build_agent_state()` dynamically creates AgentState (BaseAgentState
subclass + plugin-contributed fields) at graph build time. All plugin
read/write goes through `PluginStateHandle`, not directly touching
`ava.state` / `ava.state_update` (framework-internal slots).

Usage (in plugin's plugin.py):

    from agent.state import register_plugin_state
    from pydantic import BaseModel, Field
    from typing import Annotated

    def _set_union(old: set[str], new: set[str]) -> set[str]:
        return old | new

    class MyPluginState(BaseModel):
        counter: int = Field(default=0)
        seen: Annotated[set[str], _set_union] = Field(default_factory=set)

    state_handle = register_plugin_state(MyPluginState)
    # state_handle: PluginStateHandle[MyPluginState]
    #   .read() -> MyPluginState (typed snapshot, reflects same-turn writes)
    #   .update({"counter": 1, "seen": {"x"}})  (validated, LangGraph-reducer merged)
"""

import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from agent.messages_guard import guarded_add_messages

# The five nested sub-state channel models live in agent/state_channels
# (issue #156 — this module sat at the 800-line ceiling). They are RE-EXPORTED
# here, never re-defined: LangGraph checkpoints written before the split carry
# ("agent.state", "<Name>") ext envelopes, and the serializer resolves them by
# module attribute lookup — so old checkpoints keep deserializing only while
# these names stay importable from agent.state. New checkpoints carry
# ("agent.state_channels", "<Name>") and are allowlisted in
# shared/checkpoint_serde.py alongside the legacy pairs.
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
from shared import plugin_contributions
from shared.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from shared.plugin_context import current_plugin_name

AttachEntry = _AttachEntry


class BaseAgentState(BaseModel):
    """Framework static state — base fields all agents have.

    Flat fields (messages / halted / update_initiated) sit at the top level;
    compaction, attachments, passive-recall, and pending-context-reset
    bookkeeping are grouped into nested sub-states, each its own LangGraph
    channel holding a BaseModel.
    """

    # The messages channel runs the guarded reducer: add_messages + the
    # append-only invariant (user ruling 2026-08-13, task #1256 — only a
    # full wipe, a tail append, or modifying the last message is allowed;
    # see agent/messages_guard.py). Every persisted mutation — nodes,
    # hooks, plugins, boot repair, compaction — funnels through it at
    # commit time.
    messages: Annotated[list[AnyMessage], guarded_add_messages] = Field(default_factory=list)
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


# ── BaseAgentState field snapshot (for register_plugin_state field-name dispatch) ──

_BASE_FIELDS: frozenset[str] = frozenset(BaseAgentState.model_fields.keys())


# Two-layer dict:
#   _EXTRA_FIELDS: prefixed key → (annotation, FieldInfo)
#       stuffed into build_agent_state() namespace so LangGraph and Pydantic
#       see all plugin fields
#   _PLUGIN_NAMESPACE_FIELDS: plugin_name → set[original field name (no prefix)]
#       lets AgentState.__getattr__ know which prefixed fields belong to which
#       plugin when returning `state.<plugin>` SimpleNamespace (cross-plugin isolation)

_StateFieldSpec = tuple[Any, FieldInfo]
_EXTRA_FIELDS: dict[str, _StateFieldSpec] = {}
_PLUGIN_NAMESPACE_FIELDS: dict[str, set[str]] = {}

# Core keys a plugin may declare/write: only `messages` (its add_messages
# reducer defines the merge contract; _exec_notes.merge_exec_notes combines a
# plugin's messages delta with the exec ToolMessage — tool result first,
# notes after, per the Anthropic-compat adjacency constraint). Every other
# BaseAgentState field is framework-managed per turn (halted / turn_active /
# exit_requested / turn_idle / restart_requested / update_initiated / compact / memory / context_reset /
# capabilities):
# declaring one is rejected at register_plugin_state, and a direct
# ava.state_update write to one is rejected by _validate_plugin_state_keys.
_PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = frozenset({"messages"})

# Base fields explicitly declared by some plugin (writing same-named field in
# BaseModel). Used to distinguish "plugin explicitly updates base channel"
# (legal — currently only `messages`) vs "plugin missing-prefix typo
# accidentally stuffing base channel" (rejected): without declaration, writing
# a base field → _validate_plugin_state_keys rejects as typo.
_BASE_FIELD_DECLARED: set[str] = set()

# BaseAgentState built-in fields — plugins can modify exactly one of them:
# `messages` (only when the plugin declares it in its own BaseModel with the
# exact BaseAgentState annotation, including the add_messages reducer;
# register_plugin_state checks). Every other core key (halted / turn_active /
# exit_requested / turn_idle / restart_requested / update_initiated / compact / memory / context_reset /
# capabilities) is framework-managed every turn: declaring one raises at
# registration, and
# _BASE_FIELD_DECLARED tracks the declared (messages-only) set so a direct
# write to any other base channel = plugin missing a prefix typo (writing
# "compact" instead of "ava_myplugin__compact"), which would silent-clobber
# this turn's ToolMessage / lifecycle signal / compaction state (Python dict
# literal duplicate-key: last write wins); must blow up immediately.
#
# Derived from BaseAgentState.model_fields (via state._BASE_FIELDS) so the guard
# tracks the base as it grows/shrinks — no hardcoded list to drift (I-8).
_BASE_STATE_FIELDS: frozenset[str] = _BASE_FIELDS


def _validate_plugin_state_keys(update: dict[str, Any], state_cls: type[Any]) -> dict[str, Any]:
    """fail-fast: plugin writing to ava.state_update with illegal keys must raise.

    Two classes of abuse raise — CLAUDE.md "fail-fast / no silent fallback":
    1. Base field written but the plugin did not explicitly declare it in
       BaseModel → missing prefix typo
    2. Key not in state schema → LangGraph reducer silently drops outside
       schema; plugin author typos have no diagnostic pointer

    Explicitly declared base fields (`_BASE_FIELD_DECLARED`) are allowed:
    plugin writing via PluginStateHandle.update({"messages": [...]}) to the
    base channel is a legitimate path.

    Runs before exec_node returns, so plugin errors blow up that turn with traceback.
    """
    if not update:
        return update
    base_clash = set(update) & _BASE_STATE_FIELDS
    illegal_base = base_clash - _BASE_FIELD_DECLARED
    if illegal_base:
        raise ValueError(
            f"plugin wrote undeclared base field to ava.state_update: {sorted(illegal_base)} — "
            f"framework core keys are managed by the framework every turn; only "
            f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable, and only when declared "
            f"in the plugin's own BaseModel with the exact BaseAgentState annotation. "
            f"Missing prefix typo? Declared: {sorted(_BASE_FIELD_DECLARED) or '<empty>'}"
        )
    # Legal keys = all field names in the state schema (including base — already validated through illegal_base).
    # Previously used `model_fields - _BASE_STATE_FIELDS` to exclude base; now allowing declared
    # base fields to be written, no longer excluded.
    known = set(state_cls.model_fields.keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    unknown = set(update) - known
    if unknown:
        raise ValueError(
            f"plugin wrote unregistered key to ava.state_update: {sorted(unknown)} — "
            f"fields not declared via register_plugin_state are silently dropped by the "
            f"LangGraph reducer in Command(update=). Known plugin fields: "
            f"{sorted(known) or '<empty>'}"
        )
    return update


# Whole classes registered via register_plugin_state — the msgpack allowlist
# (checkpoint_msgpack_allowlist) must cover them: a plugin field whose value is
# a BaseModel instance (rather than a plain set/str/bool) serializes as a
# pydantic-v2 ext object into the checkpoint and is rejected without an
# explicit registration.
_PLUGIN_STATE_CLASSES: set[type[BaseModel]] = set()


# LangGraph style: `Annotated[T, reducer_fn]` stuffs reducer into Pydantic
# FieldInfo.metadata, same as `messages: Annotated[list[AnyMessage], add_messages]`
# on BaseAgentState. When plugin fields don't declare a reducer, default is
# last-value (overwrite) — same semantics as LangGraph LastValue channel.


# Canonical sentinel for the messages-channel reducer in annotation
# comparisons (see _messages_annotation_key).
_MESSAGES_REDUCER = object()


def _messages_annotation_key(annotation: Any) -> tuple[Any, ...]:
    """Canonical key for comparing base-field annotations: the messages
    reducer may be spelled `add_messages` (the contract plugins declare in
    their own BaseModel) or `guarded_add_messages` (the channel's actual
    reducer, task #1256 — add_messages plus the append-only invariant); the
    two are the same contract. Anything else compares as-is and differs."""
    meta = getattr(annotation, "__metadata__", None)
    if meta:
        origin = getattr(annotation, "__origin__", None)
        return (
            origin,
            *(
                _MESSAGES_REDUCER if m is add_messages or m is guarded_add_messages else m
                for m in meta
            ),
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
        if callable(m) and not isinstance(m, type):
            # A plugin declaring the base `messages` field spells the contract
            # annotation `add_messages`; the channel's actual reducer is the
            # guarded wrapper (same semantics + the append-only invariant,
            # task #1256). Route the working-copy merge through the guard too,
            # so an in-turn plugin violation fails inside execute_code
            # instead of only at commit.
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
    if reducer is add_messages or reducer is guarded_add_messages:
        acc_list = acc if isinstance(acc, list) else [acc]
        new_list = new if isinstance(new, list) else [new]
        return acc_list + new_list
    return reducer(acc, new)


class PluginStateHandle[T: BaseModel]:
    """Typed read/write handle for plugin state. Returned by `register_plugin_state`.

    All plugin state operations go through the handle; **do not** directly
    touch `ava.state` / `ava.state_update` (the latter is marked framework-
    internal). API shape matches LangGraph Command(update=dict):

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

    Base field reuse: if a plugin BaseModel declares a field with the same
    name as one in BaseAgentState (types matching exactly, validated at
    register_plugin_state entry), the handle routes those fields to the
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
        #   — normal register_plugin_state path always passes name).
        self._channel_keys: dict[str, str] = {}
        for name in cls.model_fields:
            if name in _BASE_FIELDS:
                self._channel_keys[name] = name
            else:
                self._channel_keys[name] = f"{plugin_name}__{name}" if plugin_name else name
        # Reducer per field. Base fields must declare exactly as BaseAgentState
        # (including Annotated reducer; register_plugin_state entry already
        # validates), so extracting what the plugin wrote gives the same
        # reducer as base; no need to look up base separately.
        self._reducers: dict[str, Callable[[Any, Any], Any]] = {
            name: _resolve_reducer(field) for name, field in cls.model_fields.items()
        }

    def read(self) -> T:
        """Current state snapshot. Reflects all `update()` calls in same turn.

        Raises:
            PluginStateOutsideTurnError: `ava.state` slot not injected (called outside exec turn).
        """
        import ava  # lazy import: avoid circular (ava imports agent.state via plugin loading)
        from ava._boot import validate_external_identity

        validate_external_identity()

        if ava.state is None:
            raise ava.PluginStateOutsideTurnError(
                f"PluginStateHandle[{self._cls.__name__}].read() called outside exec turn—"
                f"ava.state only valid inside execute_code (the exec turn)."
            )
        return self._cls.model_validate(
            {f: getattr(ava.state, self._channel_keys[f]) for f in self._cls.model_fields}
        )

    def update(self, delta: dict[str, Any]) -> None:
        """Apply field updates. Same API shape as LangGraph Command(update=dict).

        Each field's reducer merges `current ⊕ delta`; the result is written
        to the `ava.state` working copy (immediately visible to `read()`
        within this turn) + raw delta accumulated into `ava.state_update`
        (at turn end, LangGraph commits and the reducer runs again to get
        the final value).

        Keys use plugin-local field names (no prefix). The handle internally
        dispatches base/plugin to the correct channel.

        Raises:
            PluginStateOutsideTurnError: slot not injected (called outside exec turn).
            ValueError: delta contains a key outside the BaseModel schema (plugin author typo).
        """
        import ava
        from ava._boot import validate_external_identity

        validate_external_identity()

        if ava.state is None or ava.state_update is None:
            raise ava.PluginStateOutsideTurnError(
                f"PluginStateHandle[{self._cls.__name__}].update() called outside exec turn—"
                f"ava.state_update only valid inside execute_code (the exec turn)."
            )
        for field, new in delta.items():
            if field not in self._cls.model_fields:
                raise ValueError(
                    f"PluginStateHandle[{self._cls.__name__}].update: unknown field "
                    f"{field!r} (schema declares {sorted(self._cls.model_fields)})"
                )
            channel_key = self._channel_keys[field]
            current = getattr(ava.state, channel_key)
            merged = self._reducers[field](current, new)
            setattr(ava.state, channel_key, merged)  # working copy synchronously visible
            # Accumulate the raw delta into ava.state_update. The previous
            # raw-overwrite dropped every earlier delta to a reducer field in
            # one turn: two `update({"seen": {"a"}})` + `update({"seen":
            # {"b"}})` committed only {"b"}, while read() inside the turn saw
            # both — the classic silent-loss shape (2026-08-08 audit,
            # cc-backend-runtime P1). For last-value fields the reducer is
            # overwrite, so the accumulation collapses to the latest delta
            # exactly as before (see `_accumulate_delta` for the merge rule).
            if channel_key in ava.state_update:
                ava.state_update[channel_key] = _accumulate_delta(
                    ava.state_update[channel_key], new, self._reducers[field]
                )
            else:
                ava.state_update[channel_key] = new


def _annotation_text(annotation: Any) -> str:
    """`str` / `set[str]` / `str | None` — an annotation spelled the way its
    author wrote it, for the contribution ledger's one-line detail (a bare class
    reprs as `<class 'str'>`, which is noise in a catalog)."""
    return annotation.__name__ if isinstance(annotation, type) else repr(annotation)


def register_plugin_state[T: BaseModel](cls: type[T]) -> PluginStateHandle[T]:
    """Register plugin state and return a typed read/write handle.

    In a plugin's `plugin.py`:

        class MyState(BaseModel):
            counter: int = 0
            ...
        state_handle = register_plugin_state(MyState)

    The handle is then used to read/write the plugin's own state (see
    `PluginStateHandle` docstring). The framework side dispatches to
    LangGraph channels by field name:

    - Field name ∈ BaseAgentState → no prefix, shares the same channel
      with base. Only `messages` is plugin-writable (`_PLUGIN_WRITABLE_BASE_FIELDS`);
      declaring any other core key (halted / update_initiated / compact /
      memory / context_reset / capabilities) raises — those are
      framework-managed every turn. For `messages`, type must match base
      exactly (including Annotated reducer — the messages reducer may be
      spelled either `add_messages` or the guarded wrapper, same contract),
      otherwise raise — "plugin declares it will modify this base field"
      contract; not allowed to silently change types. The declaration is recorded in
      `_BASE_FIELD_DECLARED` so the framework's state_update key validation
      lets it through, and the exec node merges the plugin's messages delta
      with its own ToolMessage delta.
    - Field name ∉ base → auto-prefixed `<plugin>__<field>` into the
      dynamic AgentState; plugin-private channel; two plugins with the
      same prefixed name and different types → raise.

    Args:
        cls: ordinary `pydantic.BaseModel` subclass, **not** inheriting
            BaseAgentState — BaseAgentState field names are automatically
            detected by dispatching; plugins just write "ordinary BaseModel".

    Raises:
        TypeError: cls is not a BaseModel subclass — plugin author misused dataclass / plain class.
        ValueError: field name ∈ base but annotation doesn't match base (type swap).
        ValueError: field name ∉ base but another plugin already registered the same prefixed name with conflicting type.
    """
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(
            f"register_plugin_state expects BaseModel subclass, got {cls!r} — "
            f"plugin author writes `class FooState(BaseModel): ...` then registers."
        )

    current = current_plugin_name()

    _PLUGIN_STATE_CLASSES.add(cls)

    for name, model_field in cls.model_fields.items():
        # Pydantic v2 splits `Annotated[T, ...metadata]` into
        # `model_field.annotation` (bare T) + `model_field.metadata`
        # (Annotated's extras list); we reconstruct the full Annotated to
        # compare with the original Annotated on BaseAgentState.model_fields
        # (BaseAgentState declares `Annotated[list, guarded_add_messages]`; the
        # comparison normalizes the two spellings — see _messages_annotation_key).
        #
        # Don't read `cls.__annotations__` — under `from __future__ import
        # annotations`, plugin modules have strings (`"Annotated[set[str],
        # _my_reducer]"`); Pydantic `__init_subclass__` already
        # get_type_hints-eval'd strings into real types and stuffed them
        # into model_field (prerequisite: plugin module registered into
        # sys.modules so get_type_hints can find globals; see
        # `agent/graph/_build.py:_load_extensions`). Reconstructing from
        # model_field avoids forward-ref string residuals and avoids
        # NameErrors during get_type_hints(cls) caused by missing symbols
        # in intermediate base classes' namespaces.
        if model_field.metadata:
            raw_annotation = Annotated[model_field.annotation, *model_field.metadata]
        else:
            raw_annotation = model_field.annotation

        if name in _BASE_FIELDS:
            if name not in _PLUGIN_WRITABLE_BASE_FIELDS:
                raise ValueError(
                    f"plugin {current!r} declared core state field {name!r} — only "
                    f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable among the "
                    f"framework core keys; {name!r} is framework-managed every turn "
                    f"(write a private <plugin>__{name} field or contribute via a hook instead)"
                )
            base_raw = BaseAgentState.__annotations__[name]
            if _messages_annotation_key(raw_annotation) != _messages_annotation_key(base_raw):
                raise ValueError(
                    f"plugin {current!r} declared base field '{name}' with type "
                    f"{raw_annotation!r} differing from BaseAgentState's {base_raw!r} — "
                    f"declaring a base field is a contract 'plugin will modify this field'; "
                    f"silent type swaps not allowed."
                )
            # Base field shared: BaseAgentState already has the full
            # definition (including reducer); no need to stuff into
            # _EXTRA_FIELDS — build_agent_state won't rewrite it.
            # Recorded in _BASE_FIELD_DECLARED so _validate_plugin_state_keys
            # treats this base channel as a legal write target (writing base
            # fields without declaration is still rejected as typo).
            _BASE_FIELD_DECLARED.add(name)
            plugin_contributions.record(
                "state", name, detail=f"{cls.__name__}.{name}: base channel, co-written"
            )
            continue

        prefixed = f"{current}__{name}" if current else name
        if prefixed in _EXTRA_FIELDS:
            existing_annotation, _ = _EXTRA_FIELDS[prefixed]
            if existing_annotation != raw_annotation:
                raise ValueError(
                    f"state field {prefixed!r} type conflict: "
                    f"{existing_annotation!r} vs {raw_annotation!r}"
                )
        else:
            _EXTRA_FIELDS[prefixed] = (raw_annotation, model_field)
            plugin_contributions.record(
                "state",
                prefixed,
                detail=f"{cls.__name__}.{name}: {_annotation_text(raw_annotation)}",
            )

        if current is not None:
            _PLUGIN_NAMESPACE_FIELDS.setdefault(current, set()).add(name)

    # Same plugin with no fields must also record an empty set — so
    # `state.<plugin>` returns an empty SimpleNamespace rather than
    # AttributeError, so the "plugin declared a class but only writes base
    # fields" case can also use `ava.state.<plugin>` to get a consistent
    # view (empty, but exists).
    if current is not None and current not in _PLUGIN_NAMESPACE_FIELDS:
        _PLUGIN_NAMESPACE_FIELDS[current] = set()

    return PluginStateHandle(cls, current)


def clear_plugin_registrations() -> None:
    """Reset all plugin-registered state fields — called by
    `_load_extensions` on entry to ensure that multiple reloads (test
    fixture / dev hot-reload) don't accumulate ghost state from previous
    registrations. Also clears system prompt sections, context notes, hook
    registrations, SDK namespaces, plugin configs, plugin flag declarations
    (cross-module imports), and the attribution ledger those registrations write
    to at the same point.

    `_BASE_FIELDS` untouched (BaseAgentState is framework-fixed).
    """
    _EXTRA_FIELDS.clear()
    _PLUGIN_NAMESPACE_FIELDS.clear()
    _PLUGIN_STATE_CLASSES.clear()
    _BASE_FIELD_DECLARED.clear()
    # avoid circular import: lazy import inside the function for cross-module reset points
    import ava
    import ava._skill_sources
    from agent.graph._context_notes import _CONTEXT_NOTES, _FRAMEWORK_NOTE_COUNT
    from agent.graph._system_prompt import _FRAMEWORK_SECTION_COUNT, _SYSTEM_PROMPT_SECTIONS
    from agent.hooks._registry import HOOKS
    from shared.plugin_config_registry import clear_plugin_configs
    from shared.plugin_flags import clear_plugin_flags

    # Keep the framework-owned sections / context notes (registered once at
    # module import); drop only the plugin-contributed tails.
    del _SYSTEM_PROMPT_SECTIONS[_FRAMEWORK_SECTION_COUNT:]
    del _CONTEXT_NOTES[_FRAMEWORK_NOTE_COUNT:]
    for hook_list in HOOKS.values():
        hook_list.clear()
    clear_plugin_configs()
    clear_plugin_flags()
    plugin_contributions.clear()
    ava.clear_registered_namespaces()
    ava._extend.clear_wraps()
    ava._skill_sources.clear()


def _plugin_namespace_view(state: BaseAgentState, plugin: str) -> SimpleNamespace:
    """Construct the SimpleNamespace view for `state.<plugin>` — auto-strips
    `<plugin>__` prefix.

    `__getattr__` entry point uses this; extracted as a helper to let tests
    stub directly.
    """
    fields = _PLUGIN_NAMESPACE_FIELDS.get(plugin)
    if fields is None:
        # Plugin hasn't registered state → list known plugin names so typos
        # are immediately visible. Base fields (messages/halted) don't
        # belong to any plugin; read directly via ava.state.messages.
        known = sorted(_PLUGIN_NAMESPACE_FIELDS.keys())
        raise AttributeError(
            f"ava.state.{plugin} does not exist — plugin {plugin!r} hasn't called "
            f"register_plugin_state, or plugin name typo. Known plugins: {known or '<empty>'}"
        )
    prefix = f"{plugin}__"
    return SimpleNamespace(**{name: getattr(state, f"{prefix}{name}") for name in fields})


def build_agent_state() -> type[BaseAgentState]:
    """Build dynamic AgentState: BaseAgentState subclass + all plugin-declared fields.

    Uses subclass (rather than `pydantic.create_model`) to guarantee
    BaseAgentState's field (messages/halted) FieldInfo objects are exactly
    the same — LangGraph channel type comparison relies on FieldInfo equality
    (__eq__); create_model would rebuild FieldInfo and != would treat them
    as different types.

    The new class's `__module__` is set to this module, and the name is
    bound back to sys.modules so Pydantic's `model_rebuild()` or
    `get_type_hints()` can find the "AgentState" name in this module
    (Pydantic indexes by `<module>.<qualname>`).

    The AgentState class has `__getattr__`: `state.<plugin_name>` returns a
    SimpleNamespace view (auto-strips `<plugin>__` prefix). Lets agents
    write `ava.state.ava_code.cwd` rather than `ava.state.ava_code__cwd`;
    plugin-private namespaces also isolated cross-plugin.
    """
    if not _EXTRA_FIELDS and not _PLUGIN_NAMESPACE_FIELDS:
        # Without plugin fields, also can't directly return BaseAgentState
        # — it has no plugin namespace __getattr__; but actually "no
        # plugin" also means no one will use state.<plugin>, so returning
        # BaseAgentState is simpler and reuses the LangGraph channel old
        # implementation path.
        return BaseAgentState

    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {"__annotations__": annotations}
    for name, (annotation, field) in _EXTRA_FIELDS.items():
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
        # Note: _plugin_namespace_view reads module-level _PLUGIN_NAMESPACE_FIELDS
        # rather than a build-time snapshot — clear_plugin_registrations
        # clears it, but after that the graph also rebuilds the AgentState
        # class, so there's no "old state instance hitting new dict" race.
        if item.startswith("_"):
            raise AttributeError(item)
        return _plugin_namespace_view(self, item)

    namespace["__getattr__"] = _state_getattr

    cls = type("AgentState", (BaseAgentState,), namespace)
    cls.__module__ = __name__
    sys.modules[__name__].__dict__["AgentState"] = cls
    return cls


# ── Backwards compat: AgentState = BaseAgentState (used in static contexts / type annotations) ──
# After build_agent_state() is called, this is overwritten by the real
# subclass; at module load time it is an alias.
AgentState = BaseAgentState


# ── Checkpoint msgpack allowlist ──


def checkpoint_msgpack_allowlist() -> frozenset[tuple[str, str]]:
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

    The dynamic `AgentState` subclass is listed by the name `build_agent_state`
    binds into this module ("AgentState"); the module-level alias has the same
    name so the entry is correct regardless of build order. Plugin classes
    registered via `register_plugin_state` are included automatically — a
    plugin field holding a BaseModel instance crosses the checkpointer as that
    class and would otherwise be blocked (degraded to a plain dict) the moment
    the allowlist replaces the permissive default.

    Consumers: `services/agent_host/daemon.py::_build_checkpointer` (shared host saver) and
    any embedding checkpoint saver.
    """
    entries: set[tuple[str, str]] = set(STATIC_CHECKPOINT_MSGPACK_TYPES)
    for cls in _PLUGIN_STATE_CLASSES:
        entries.add((cls.__module__, cls.__name__))
    return frozenset(entries)


def build_checkpoint_serde() -> JsonPlusSerializer:
    """JsonPlusSerializer with the framework checkpoint allowlist — pass as
    `serde=` when constructing the hosted LangGraph checkpointer."""
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())
