"""Fast path for the append-only guard's read fold (B2 fix, task #3180).

``_try_pure_append`` classifies a write before the full diff check: no
``RemoveMessage`` and no id already present in ``current`` means the write
can only lengthen the list at the tail — exactly the legal append class —
so it is merged directly in O(len(delta)). Every other shape falls through
to the full diff validation, unchanged. ``guarded_delta_reducer`` threads
the classification (and the id index it needs) across writes, turning an
append-dominated fold from O(N*W) into O(N + K).

The hard gate: the fast path is acceleration only. These tests pin it.
- Construction equality — for every accepted shape, a fast-enabled fold
  and the same fold with the fast path disabled (the pre-change code path)
  yield structurally equal values.
- Decision equality — violations still raise, with and without the fast
  path.
- The fast path is really taken for pure appends (the full validators are
  armed to blow up if consulted), and the fold's acceleration state is
  batching-invariant, deterministic, and never mutates the caller's base.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agent import messages_guard as guard
from agent.messages_guard import (
    MessagesMutationError,
    guarded_add_messages,
    guarded_delta_reducer,
)

StateBuilder = Callable[[], tuple[list[Any], list[Any]]]


def _msgs(*specs: tuple[str, str]) -> list[AnyMessage]:
    return [HumanMessage(content=c, id=i) for i, c in specs]


def _ids(messages: list[AnyMessage]) -> list[str | None]:
    return [m.id for m in messages]


def _dump(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    return [m.model_dump() for m in messages]


def _fast_path_disabled(*_args: Any, **_kwargs: Any) -> None:
    """A ``_try_pure_append`` replacement that always declines — the
    pre-change code path (full diff validation only)."""


def _fold(build: StateBuilder, *, fast: bool) -> list[AnyMessage]:
    """One fold run; ``fast=False`` disables the pure-append classification."""
    if fast:
        return guarded_delta_reducer(*build())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(guard, "_try_pure_append", _fast_path_disabled)
        return guarded_delta_reducer(*build())


# ── accepted shapes: fast and slow must agree ───────────────────────────────


def _append_single() -> tuple[list[AnyMessage], list[Any]]:
    return _msgs(("b0", "zero"), ("b1", "one")), [[HumanMessage(content="n0", id="n0")]]


def _append_batch() -> tuple[list[AnyMessage], list[Any]]:
    return _msgs(("b0", "zero")), [
        [HumanMessage(content="n0", id="n0"), AIMessage(content="n1", id="n1")]
    ]


def _append_duplicate_ids_in_one_write() -> tuple[list[AnyMessage], list[Any]]:
    """A duplicate id inside one write keeps its last value at the first
    slot — the exact ``add_messages`` replace-in-place semantics."""
    return _msgs(("b0", "zero")), [
        [HumanMessage(content="v1", id="dup"), HumanMessage(content="v2", id="dup")]
    ]


def _append_to_empty_history() -> tuple[list[AnyMessage], list[Any]]:
    return [], [[HumanMessage(content="n0", id="n0")]]


def _bare_message_write() -> tuple[list[AnyMessage], list[Any]]:
    """A write that is a single message-like, not a list, is one message."""
    return _msgs(("b0", "zero")), [HumanMessage(content="n0", id="n0")]


def _empty_write() -> tuple[list[AnyMessage], list[Any]]:
    return _msgs(("b0", "zero")), [[]]


def _append_then_modify_last_then_append() -> tuple[list[AnyMessage], list[Any]]:
    """A slow-path write mid-fold discards the fast path's index; the next
    append rebuilds it and must still agree with the slow-only fold."""
    return _msgs(("b0", "zero"), ("b1", "one")), [
        [HumanMessage(content="n0", id="n0")],
        [HumanMessage(content="n0-fixed", id="n0")],
        [HumanMessage(content="n1", id="n1")],
    ]


def _wipe_then_append() -> tuple[list[AnyMessage], list[Any]]:
    """The full-wipe class is a slow-path write; appends after it agree too."""
    base = _msgs(("b0", "zero"), ("b1", "one"))
    return base, [
        [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            base[0],
            HumanMessage(content="r0", id="r0"),
        ],
        [HumanMessage(content="n0", id="n0")],
    ]


def _delta_form_current_then_append() -> tuple[list[Any], list[Any]]:
    """A delta-form current (hook-runner co-write merge) stays on the
    untouched path — the marker flag must keep every write there."""
    base = _msgs(("b0", "zero"), ("b1", "one"))
    delta_current: list[Any] = [RemoveMessage(id="b1"), *base]
    return delta_current, [[HumanMessage(content="n0", id="n0")]]


def _chunk_write() -> tuple[list[AnyMessage], list[Any]]:
    """Message chunks coerce to full messages on both paths."""
    return _msgs(("b0", "zero")), [[AIMessageChunk(content="chunk", id="ck0")]]


def _dict_write() -> tuple[list[AnyMessage], list[Any]]:
    """Raw dict message-likes coerce through the same conversion chain."""
    return _msgs(("b0", "zero")), [[{"role": "user", "content": "raw", "id": "d0"}]]


_ACCEPT_BUILDERS = [
    _append_single,
    _append_batch,
    _append_duplicate_ids_in_one_write,
    _append_to_empty_history,
    _bare_message_write,
    _empty_write,
    _append_then_modify_last_then_append,
    _wipe_then_append,
    _delta_form_current_then_append,
    _chunk_write,
    _dict_write,
]


@pytest.mark.parametrize("build", _ACCEPT_BUILDERS, ids=lambda f: f.__name__)
def test_fold_result_matches_slow_path(build: StateBuilder) -> None:
    fast = _fold(build, fast=True)
    slow = _fold(build, fast=False)
    assert _dump(fast) == _dump(slow)
    assert _ids(fast) == _ids(slow)


# ── rejected shapes: violations must raise either way ───────────────────────


def _edit_middle_message() -> tuple[list[AnyMessage], list[Any]]:
    base = _msgs(("b0", "zero"), ("b1", "one"), ("b2", "two"))
    return base, [[HumanMessage(content="CHANGED", id="b1")]]


def _targeted_removal() -> tuple[list[AnyMessage], list[Any]]:
    base = _msgs(("b0", "zero"), ("b1", "one"), ("b2", "two"))
    return base, [[RemoveMessage(id="b0")]]


def _delete_last_and_append() -> tuple[list[AnyMessage], list[Any]]:
    base = _msgs(("b0", "zero"), ("b1", "one"))
    return base, [[RemoveMessage(id="b1"), HumanMessage(content="new", id="n0")]]


def _wipe_reorder_survivors() -> tuple[list[AnyMessage], list[Any]]:
    base = _msgs(("b0", "zero"), ("b1", "one"))
    return base, [[RemoveMessage(id=REMOVE_ALL_MESSAGES), base[1], base[0]]]


def _wipe_alter_survivor() -> tuple[list[AnyMessage], list[Any]]:
    base = _msgs(("b0", "zero"), ("b1", "one"))
    tampered = HumanMessage(content="TAMPERED", id="b0")
    return base, [[RemoveMessage(id=REMOVE_ALL_MESSAGES), tampered]]


@pytest.mark.parametrize(
    "build",
    [
        _edit_middle_message,
        _targeted_removal,
        _delete_last_and_append,
        _wipe_reorder_survivors,
        _wipe_alter_survivor,
    ],
    ids=lambda f: f.__name__,
)
def test_violations_raise_with_and_without_fast_path(build: StateBuilder) -> None:
    with pytest.raises(MessagesMutationError):
        _fold(build, fast=True)
    with pytest.raises(MessagesMutationError):
        _fold(build, fast=False)


# ── the fast path is real, and its acceleration state is sound ──────────────


def test_pure_appends_never_consult_the_full_validators() -> None:
    """Prove the fast path actually runs: arm the full validators to blow up
    if a pure append reaches them."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("full-path validation ran for a pure append")

    writes: list[Any] = [
        [HumanMessage(content="n0", id="n0")],
        [HumanMessage(content="n1", id="n1")],
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(guard, "validate_messages_mutation", forbidden)
        mp.setattr(guard, "validate_rebuild", forbidden)
        folded = guarded_delta_reducer(_msgs(("b0", "zero")), writes)
        assert _ids(folded) == ["b0", "n0", "n1"]
        # the single-application entry point takes the same fast path
        merged = guarded_add_messages(_msgs(("c0", "zero")), [HumanMessage(content="n0", id="n0")])
        assert _ids(merged) == ["c0", "n0"]


def test_fast_append_keeps_survivor_identity_and_never_mutates_the_base() -> None:
    base = _msgs(("b0", "zero"), ("b1", "one"))
    n0 = HumanMessage(content="n0", id="n0")
    folded = guarded_delta_reducer(base, [[n0]])
    assert _ids(base) == ["b0", "b1"]  # the caller's list is untouched
    assert folded is not base
    assert folded[0] is base[0] and folded[1] is base[1]
    assert folded[2] is n0


def test_split_fold_equals_one_shot_fold_with_fast_path() -> None:
    xs: list[Any] = [[HumanMessage(content="n0", id="n0")]]
    ys: list[Any] = [[HumanMessage(content="n1", id="n1")]]
    one_shot = guarded_delta_reducer(_msgs(("b0", "zero")), xs + ys)
    split = guarded_delta_reducer(guarded_delta_reducer(_msgs(("b0", "zero")), xs), ys)
    assert _dump(split) == _dump(one_shot)


def test_split_fold_with_a_slow_write_still_matches() -> None:
    xs: list[Any] = [
        [HumanMessage(content="n0", id="n0")],
        [HumanMessage(content="n0-fixed", id="n0")],  # modify last — slow write
    ]
    ys: list[Any] = [[HumanMessage(content="n1", id="n1")]]
    one_shot = guarded_delta_reducer(_msgs(("b0", "zero")), xs + ys)
    split = guarded_delta_reducer(guarded_delta_reducer(_msgs(("b0", "zero")), xs), ys)
    assert _dump(split) == _dump(one_shot)


def test_replaying_the_same_writes_is_deterministic() -> None:
    def build_writes() -> list[Any]:
        return [
            [HumanMessage(content="n0", id="n0")],
            [HumanMessage(content="n0-fixed", id="n0")],
            [HumanMessage(content="n1", id="n1")],
            [HumanMessage(content="n2", id="n2"), AIMessage(content="n3", id="n3")],
        ]

    first = guarded_delta_reducer(_msgs(("b0", "zero")), build_writes())
    second = guarded_delta_reducer(_msgs(("b0", "zero")), build_writes())
    assert _dump(first) == _dump(second)
    assert _ids(first) == ["b0", "n0", "n1", "n2", "n3"]


def test_missing_ids_stay_on_the_slow_path() -> None:
    """A write with no id cannot be classified (``add_messages`` assigns a
    fresh UUID in place) — both runs take the slow path, and the fold is
    unchanged; the append after it is fast again."""

    def build() -> tuple[list[AnyMessage], list[Any]]:
        return _msgs(("b0", "zero")), [
            [HumanMessage(content="anonymous")],
            [HumanMessage(content="n0", id="n0")],
        ]

    fast = _fold(build, fast=True)
    slow = _fold(build, fast=False)
    assert [type(m).__name__ for m in fast] == [type(m).__name__ for m in slow]
    assert [m.content for m in fast] == ["zero", "anonymous", "n0"]
    assert _ids(fast)[0] == _ids(slow)[0] == "b0"
    assert _ids(fast)[2] == _ids(slow)[2] == "n0"
    # a fresh UUID was assigned on both paths — the fast path would have
    # appended the id-less message verbatim
    assert fast[1].id is not None and slow[1].id is not None


def test_degenerate_none_inputs_keep_add_messages_error_contract() -> None:
    """``None`` current/delta crash exactly as ``add_messages`` does."""
    with pytest.raises(ValueError):
        guarded_add_messages(None, [HumanMessage(content="x", id="x")])
    with pytest.raises(ValueError):
        guarded_add_messages([HumanMessage(content="x", id="x")], None)


def test_single_apply_matches_add_messages_for_append_shapes() -> None:
    """The fast path stays a drop-in for ``add_messages`` on appends — the
    repo's behavior-match test, over batch and duplicate-id deltas."""
    base = _msgs(("b0", "zero"), ("b1", "one"))
    deltas: list[list[AnyMessage]] = [
        [HumanMessage(content="n0", id="n0")],
        [HumanMessage(content="v1", id="dup"), HumanMessage(content="v2", id="dup")],
        [HumanMessage(content="n0", id="n0"), AIMessage(content="n1", id="n1")],
    ]
    for delta in deltas:
        merged = guarded_add_messages(base, delta)
        expected = cast(list[AnyMessage], guard.add_messages(cast(Any, base), cast(Any, delta)))
        assert _dump(merged) == _dump(expected)


def test_unknown_id_removal_declines_fast_path_and_matches_add_messages() -> None:
    """A ``RemoveMessage`` for an id absent from the thread must not be
    classified as a new message: the fast path declines it (its
    ``RemoveMessage`` early-return), and the outcome equals ``add_messages``
    — ``ValueError`` for deleting an id that does not exist (it is not a
    no-op), the same error ``guarded_add_messages`` raises."""

    def hold() -> list[Any]:
        return [RemoveMessage(id="not-present")]

    with pytest.raises(ValueError, match="ID that doesn't exist"):
        guarded_delta_reducer(_msgs(("b0", "zero")), [hold()])
    with pytest.raises(ValueError, match="ID that doesn't exist"):
        guarded_add_messages(_msgs(("b0", "zero")), hold())
    with pytest.raises(ValueError, match="ID that doesn't exist"):
        guard.add_messages(cast(Any, _msgs(("b0", "zero"))), cast(Any, hold()))


def test_wipe_alone_declines_fast_path() -> None:
    """A lone ``RemoveMessage(REMOVE_ALL)`` is the full-wipe class, not a new
    message: the fast path declines it, and the fold yields the empty list
    (everything after the marker) — equal to the fast-disabled fold."""

    def build() -> tuple[list[Any], list[Any]]:
        return _msgs(("b0", "zero")), [[RemoveMessage(id=REMOVE_ALL_MESSAGES)]]

    folded = guarded_delta_reducer(*build())
    assert folded == []
    slow = _fold(build, fast=False)
    assert slow == []
    assert _dump(folded) == _dump(slow)
