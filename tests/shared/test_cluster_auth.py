"""Unit tests for shared/cluster_auth.py — the cluster-secret bearer."""

from shared.cluster_auth import bearer_header, verify_bearer


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


def test_session_cookie_header_persists_max_age() -> None:
    """The session cookie must carry Max-Age (mirrors token TTL): without it
    Chromium treats it as a session cookie and drops it on restart (desktop #706)."""
    from shared.cluster_auth import _SESSION_TTL, session_cookie_header

    header = session_cookie_header("tok")
    set_cookie = header["Set-Cookie"]
    assert f"Max-Age={_SESSION_TTL}" in set_cookie
    assert "HttpOnly" in set_cookie
