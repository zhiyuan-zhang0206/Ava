"""Message-list mutation hardening — the append-only invariant gatekeeper.

User ruling (2026-08-13, task #1256): the stored message list may only
change in three ways —

  1. full wipe (compaction; crash-repair rebuild) via
     ``RemoveMessage(REMOVE_ALL_MESSAGES)``
  2. append at the tail
  3. modify the last message (the one the LLM just produced)

Everything else — editing an older message's content, reordering, or
inserting new messages mid-history — is a violation. It breaks the
"model-visible means logged" invariant (a message the model saw must stay
exactly as the model saw it) and silently destroys prompt-caching hit
rates, which are extremely prefix-sensitive: a mid-history edit changes
every later prefix.

``guarded_add_messages`` wraps langgraph's ``add_messages`` and is installed
as the ``messages`` channel reducer on ``BaseAgentState`` (agent/state.py).
LangGraph invokes the channel reducer once per write at commit time with
``(checkpoint_value, delta)`` — so every persisted mutation — nodes, hooks,
plugins, the boot-pass crash repair, compaction — funnels through the guard
at the single choke point. The plugin state handle routes its working-copy
merges through the same guard, so an in-turn plugin violation fails in the
exec thread instead of only at commit.

Validation model (per the ruling):

- ``current`` containing ``RemoveMessage`` markers is itself a delta (the
  hook runner merges sibling hook deltas through the same reducer); a delta
  is not a checkpoint, so validation is skipped and the merge proceeds with
  plain ``add_messages`` semantics.
- Delta containing ``RemoveMessage(REMOVE_ALL)`` → full-wipe class: the
  result is a fresh list, subject to one constraint — every survivor (a
  message id present both before and after) must keep its exact content and
  its relative order. This is what lets the crash-repair rebuild re-list the
  old history unchanged while splicing synthetic ``[interrupted]``
  tool_results mid-list, yet still catches a rebuild that tampers with or
  reorders old messages.
- Otherwise the append-only class:
  - no deletions — every ``before`` id must survive in ``after``;
  - every survivor sits at the same index with identical content, except
    the last ``before`` message, whose content may change (the ruling's
    "modify the last message" — exec's tool-call merge and the syntax-fix
    plugin both replace it by same id);
  - new messages (ids absent from ``before``) may only form a contiguous
    suffix of ``after`` — no middle insertion.

``validate_messages_mutation`` / ``validate_rebuild`` are the pure diff
checks, unit-testable without a graph.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import (
    AnyMessage,
    RemoveMessage,
    convert_to_messages,
)
from langchain_core.messages.utils import message_chunk_to_message
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages


class MessagesMutationError(RuntimeError):
    """A messages-channel mutation violated the append-only invariant.

    Raised by the guarded reducer at commit time (propagates out of
    ``graph.ainvoke`` — fail-fast, the process dies with the message) or by
    the plugin state handle's working-copy merge (surfaces as the exec
    turn's crash output). The message names the violating index, message
    type/id, and the rule broken.
    """


def _coerce(messages: Any) -> list[AnyMessage]:
    """Coerce message-likes the same way ``add_messages`` does, so the
    fingerprints validation takes compare the very objects the reducer
    merged (for typed ``BaseMessage`` inputs this is identity — no copy)."""
    return [cast(AnyMessage, message_chunk_to_message(m)) for m in convert_to_messages(messages)]


def _contains_remove_all(delta: Any) -> bool:
    msgs: list[Any] = delta if isinstance(delta, list) else [delta]
    return any(isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES for m in msgs)


def _is_delta(current: Any) -> bool:
    """True when ``current`` is a delta (carries RemoveMessage markers) rather
    than a committed message list. A committed channel value never contains
    markers — the reducer consumes them — so their presence means the caller
    (the hook runner's co-write merge) handed us an accumulated delta."""
    msgs: list[Any] = current if isinstance(current, list) else [current]
    return any(isinstance(m, RemoveMessage) for m in msgs)


def _fingerprint(m: AnyMessage) -> dict[str, Any]:
    """Structural fingerprint of a message — everything the checkpoint stores."""
    return m.model_dump()


def _describe(m: AnyMessage) -> str:
    return f"{type(m).__name__} id={m.id!r}"


def _violation(before: list[AnyMessage], after: list[AnyMessage], detail: str) -> None:
    raise MessagesMutationError(
        f"messages-channel mutation violated the append-only invariant "
        f"(user ruling 2026-08-13, task #1256): only a full wipe "
        f"(RemoveMessage(REMOVE_ALL)), a tail append, or modifying the last "
        f"message is allowed. before={len(before)} messages, "
        f"after={len(after)} messages. Violation: {detail}"
    )


def validate_messages_mutation(before: Sequence[AnyMessage], after: Sequence[AnyMessage]) -> None:
    """Append-only class check: no deletions, survivors stay put and
    unchanged (except the last message), new messages only as a suffix.

    Pure function — unit-testable without a graph. ``before`` and ``after``
    are the message lists on either side of one reducer application.
    Raises ``MessagesMutationError`` on a violation; the caller decides
    the class (full wipe vs append-only) by inspecting the delta.
    """
    before = _coerce(before)
    after = _coerce(after)
    if not before:
        return  # cold start / empty history — anything is a fresh list
    before_by_id: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(before)}
    last_before = len(before) - 1
    seen_new = False
    for i, m in enumerate(after):
        mid = m.id
        if mid in before_by_id:
            j = before_by_id[mid]
            if seen_new:
                _violation(
                    before,
                    after,
                    f"survivor {_describe(m)} at after index {i} follows a new "
                    f"message — new messages must form a tail suffix (no "
                    f"middle insertion / reordering)",
                )
            if j != i:
                _violation(
                    before,
                    after,
                    f"survivor {_describe(m)} moved from before index {j} to "
                    f"after index {i} — earlier messages must keep their "
                    f"position (no reorder / middle insert / deletion)",
                )
            if i != last_before and _fingerprint(m) != _fingerprint(before[j]):
                _violation(
                    before,
                    after,
                    f"message {_describe(m)} at index {i} changed content — "
                    f"only the last message (index {last_before}) may be "
                    f"modified",
                )
        else:
            seen_new = True
    after_ids = {m.id for m in after}
    missing = [m for m in before if m.id not in after_ids]
    if missing:
        _violation(
            before,
            after,
            f"messages deleted: {[_describe(m) for m in missing]} — only a "
            f"full wipe (RemoveMessage(REMOVE_ALL)) may delete messages",
        )


def validate_rebuild(before: Sequence[AnyMessage], after: Sequence[AnyMessage]) -> None:
    """Full-wipe class check: survivors keep content + relative order.

    Called when the delta carried ``RemoveMessage(REMOVE_ALL)`` — the result
    is a fresh list, but any old message carried over must be untouched and
    in its original relative order (the crash-repair rebuild re-lists the
    history unchanged and splices synthetic tool_results mid-list; a
    rebuild that edits or reorders old messages is still a violation)."""
    before = _coerce(before)
    after = _coerce(after)
    if not before:
        return
    before_by_id: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(before)}
    after_idx: dict[str, int] = {cast(str, m.id): i for i, m in enumerate(after)}
    prev = -1
    for m in after:
        mid = m.id
        if mid not in before_by_id:
            continue
        j = before_by_id[mid]
        if _fingerprint(m) != _fingerprint(before[j]):
            _violation(
                before,
                after,
                f"full-wipe rebuild altered surviving message {_describe(m)} "
                f"(before index {j}) — old messages must survive a rebuild "
                f"unchanged",
            )
        if j < prev:
            _violation(
                before,
                after,
                f"full-wipe rebuild reordered surviving messages "
                f"({_describe(m)}: before index {j}, after index "
                f"{after_idx[mid]}) — survivors must keep their relative "
                f"order",
            )
        prev = j


def guarded_add_messages(current: Any, delta: Any) -> Any:
    """``add_messages`` + the append-only invariant check (see module docstring).

    Installed as the ``messages`` channel reducer; also used by the plugin
    state handle for working-copy merges. The check is skipped when
    ``current`` is itself a delta (hook-runner co-write merges) — a delta is
    not a checkpoint and carries no invariant of its own.
    """
    merged: Any = add_messages(current, delta)
    if _is_delta(current):
        return merged
    before = _coerce(current)
    after = _coerce(merged)
    if _contains_remove_all(delta):
        validate_rebuild(before, after)
    else:
        validate_messages_mutation(before, after)
    return merged
