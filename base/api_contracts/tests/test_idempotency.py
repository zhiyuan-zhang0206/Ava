import pytest

from base.api_contracts.idempotency import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    chat_message_key,
    keyed_headers,
    stored_key,
    validate_idempotency_key,
)


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


def test_keyed_headers_pair_the_key_with_the_principal_scope() -> None:
    assert keyed_headers("op-1") == {"Idempotency-Key": "op-1", SCOPE_HEADER: PRINCIPAL_SCOPE}
    with pytest.raises(ValueError, match="1 to 128"):
        keyed_headers("")


def test_stored_key_separates_credential_route_and_caller_key() -> None:
    base = stored_key("cluster", "administrator", "post", "/api/a", "k")
    assert base == stored_key("cluster", "administrator", "POST", "/api/a", "k")
    assert base.startswith("principal-v1:")
    others = {
        stored_key("mcp_client", "administrator", "POST", "/api/a", "k"),
        stored_key("cluster", "unauthenticated", "POST", "/api/a", "k"),
        stored_key("cluster", "administrator", "PATCH", "/api/a", "k"),
        stored_key("cluster", "administrator", "POST", "/api/b", "k"),
        stored_key("cluster", "administrator", "POST", "/api/a", "other"),
    }
    assert base not in others
    assert len(others) == 5


def test_chat_message_key_is_one_cluster_wide_identity_per_caller_key() -> None:
    assert chat_message_key("m-1") == stored_key(
        "cluster", "administrator", "POST", "chat-message", "m-1"
    )
    assert chat_message_key("m-1") != chat_message_key("m-2")
