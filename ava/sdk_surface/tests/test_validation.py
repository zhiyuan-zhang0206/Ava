"""Unified SDK argument validation — the trailing-comma guard.

Every ava.* entry point validates its own arguments through the shared
`ava.sdk_surface.validation` helpers (one implementation, user ruling 2026-08-28):

- string-expected arguments unwrap a one-element list/tuple whose element is a
  string (the LLM trailing-comma class that 422'd the gateway — issue #1343,
  2026-08-28 send_message agents 2697/2986);
- multi-element sequences and wrong types raise TypeError naming the parameter;
- arguments that are inherently not strings are checked strictly and never
  unwrapped.

The suite covers ① single-element tuple normalization at every entry point,
② multi-element / wrong-type TypeError, ③ zero regression of existing behavior
(str / None / Path / int values still flow through unchanged).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ava.sdk_surface.validation import coerce_str, coerce_typed

# ── helper units ─────────────────────────────────────────────────────────────


class TestCoerceStr:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("plain", "plain", id="plain-string"),
            pytest.param(("one",), "one", id="one-element-tuple"),
            pytest.param(["one"], "one", id="one-element-list"),
            pytest.param(("implicit concat",), "implicit concat", id="implicit-concatenation"),
            pytest.param(None, None, id="none-allowed"),
        ],
    )
    def test_unwraps_or_passes(self, value: object, expected: object) -> None:
        assert coerce_str(value, "x", allow_none=True) == expected

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(("a", "b"), id="multi-element-tuple"),
            pytest.param(["a", "b"], id="multi-element-list"),
            pytest.param((1,), id="non-string-element"),
            pytest.param([1], id="non-string-list-element"),
            pytest.param((), id="empty-tuple"),
            pytest.param(42, id="int"),
            pytest.param(True, id="bool"),
            pytest.param(None, id="none-rejected"),
        ],
    )
    def test_type_errors(self, value: object) -> None:
        with pytest.raises(TypeError, match="x must be a string"):
            coerce_str(value, "x")

    def test_allow_types_passes_legitimate_non_string_forms(self, tmp_path: Path) -> None:
        import datetime

        when = datetime.datetime.now(datetime.UTC)
        assert coerce_str(when, "when", allow_types=(datetime.datetime, datetime.timedelta)) is when
        delta = datetime.timedelta(minutes=5)
        assert (
            coerce_str(delta, "when", allow_types=(datetime.datetime, datetime.timedelta)) is delta
        )
        p = tmp_path
        assert coerce_str(p, "path", allow_types=(Path,)) is p

    def test_allow_types_unwraps_before_passing(self) -> None:
        import datetime

        value = coerce_str(
            ("2026-01-01T00:00:00+08:00",),
            "when",
            allow_types=(datetime.datetime, datetime.timedelta),
        )
        assert value == "2026-01-01T00:00:00+08:00"

    def test_sequence_allowed_only_for_dict_lists(self) -> None:
        """The one sequence the SDK accepts as a non-string value is the
        multimodal content-block list — an all-string array is exactly the
        shape the gateway rejects."""
        blocks = [{"type": "text", "text": "hi"}]
        assert coerce_str(blocks, "content", allow_types=(list,)) is blocks
        assert coerce_str([], "content", allow_types=(list,)) == []
        with pytest.raises(TypeError, match="content must be a string or a list of dicts"):
            coerce_str(["a", "b"], "content", allow_types=(list,))
        with pytest.raises(TypeError, match="content must be a string or a list of dicts"):
            coerce_str([1], "content", allow_types=(list,))


class TestCoerceTyped:
    def test_passes_declared_types(self) -> None:
        assert coerce_typed(5, "n", int) == 5
        assert coerce_typed(5.0, "n", (int, float)) == 5.0
        assert coerce_typed(None, "n", int, allow_none=True) is None
        assert coerce_typed([1, 2], "tags", (list, tuple)) == [1, 2]
        assert coerce_typed((1, 2), "tags", (list, tuple)) == (1, 2)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("5", int, id="string-for-int"),
            pytest.param((5,), int, id="one-element-tuple-for-int"),
            pytest.param(None, int, id="none-rejected"),
            pytest.param([5], int, id="list-for-int"),
        ],
    )
    def test_never_unwraps(self, value: object, expected: type) -> None:
        """A one-element tuple for a non-string parameter is a TypeError — no
        expansion outside the string-expected class."""
        with pytest.raises(TypeError, match="n must be"):
            coerce_typed(value, "n", expected)


# ── agents ───────────────────────────────────────────────────────────────────


# ── files ────────────────────────────────────────────────────────────────────


# ── shell ────────────────────────────────────────────────────────────────────


# ── ui ───────────────────────────────────────────────────────────────────────


# ── watcher ──────────────────────────────────────────────────────────────────


# ── web / understand ─────────────────────────────────────────────────────────


# ── memory / tasks / notices ─────────────────────────────────────────────────


# ── self / attach ────────────────────────────────────────────────────────────
