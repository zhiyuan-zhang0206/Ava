"""Unit tests for shared/cluster_auth.py — bearer and cookie primitives."""

from base64 import urlsafe_b64decode

from shared.cluster_auth import (
    bearer_header,
    new_session_id,
    session_cookie_header,
    verify_bearer,
)


def test_bearer_header_shape() -> None:
    assert bearer_header("s3cret") == {"Authorization": "Bearer s3cret"}


def test_verify_accepts_matching_bearer() -> None:
    assert verify_bearer("Bearer s3cret", "s3cret") is True


def test_verify_rejects_wrong_secret() -> None:
    assert verify_bearer("Bearer wrong", "s3cret") is False


def test_verify_rejects_missing_header() -> None:
    assert verify_bearer(None, "s3cret") is False


def test_verify_rejects_malformed_scheme() -> None:
    assert verify_bearer("s3cret", "s3cret") is False  # no "Bearer " prefix
    assert verify_bearer("Basic s3cret", "s3cret") is False


def test_verify_fails_closed_on_empty_secret() -> None:
    # an unset cluster secret never verifies — fail closed, not open.
    assert verify_bearer("Bearer ", "") is False
    assert verify_bearer("Bearer ", "") is False  # bearer_header("") would produce this


def test_header_then_verify_roundtrip() -> None:
    h = bearer_header("round-trip-secret")
    assert verify_bearer(h["Authorization"], "round-trip-secret") is True


def test_new_session_id_is_random_urlsafe_32_bytes() -> None:
    first = new_session_id()
    second = new_session_id()

    assert first != second
    assert "=" not in first
    padded = first + "=" * (-len(first) % 4)
    assert len(urlsafe_b64decode(padded)) == 32


def test_session_cookie_header_uses_explicit_ttl() -> None:
    set_cookie = session_cookie_header("tok", ttl_seconds=123)["Set-Cookie"]

    assert "Max-Age=123" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/" in set_cookie


def test_session_cookie_header_secure_flag_is_explicit() -> None:
    insecure = session_cookie_header("tok", secure=False)["Set-Cookie"]
    secure = session_cookie_header("tok", secure=True)["Set-Cookie"]

    assert "; Secure" not in insecure
    assert "; Secure" in secure
