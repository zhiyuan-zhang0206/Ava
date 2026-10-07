"""Supplied operation keys preserve their identity or fail at the boundary."""

import pytest

from base.api_contracts.idempotency import validate_idempotency_key


@pytest.mark.parametrize("value", [None, 42, ("key",), b"key"])
def test_non_string_key_is_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="must be a string"):
        validate_idempotency_key(value)


@pytest.mark.parametrize("value", ["", "x" * 129])
def test_key_length_is_bounded(value: str) -> None:
    with pytest.raises(ValueError, match="1 to 128"):
        validate_idempotency_key(value)


def test_string_subclass_preserves_exact_key() -> None:
    class Key(str):
        pass

    key = Key("caller-key")
    assert validate_idempotency_key(key) is key
    assert validate_idempotency_key(" " * 128) == " " * 128
