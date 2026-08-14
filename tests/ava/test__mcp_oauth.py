"""Unit tests for ava._mcp_oauth — token storage, callback server, client assembly.

The authorization flow itself (discovery → register → redirect → token
exchange) lives in the SDK's OAuthClientProvider (an httpx2.Auth); these tests
cover our parts: file persistence, the loopback callback capture, and the
provider wiring. An end-to-end flow against a real server is a manual/CI
smoke, not a unit test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import ava._mcp_oauth as oauth_mod

# ─── FileTokenStorage ─────────────────────────────────────────────────────


def test_token_storage_roundtrip(unit_home: Path) -> None:
    """Tokens and client info survive a write/read cycle at 0600."""
    storage = oauth_mod._FileTokenStorage("exa")
    assert storage._path == unit_home / "mcp_oauth" / "exa.json"

    token = OAuthToken(
        access_token="at-1",  # noqa: S106
        token_type="Bearer",  # noqa: S106
        expires_in=3600,
        refresh_token="rt-1",  # noqa: S106
        scope="user",
    )
    client_info = OAuthClientInformationFull(client_id="abc", client_secret=None, redirect_uris=[])

    asyncio.run(storage.set_tokens(token))
    asyncio.run(storage.set_client_info(client_info))

    got_token = asyncio.run(storage.get_tokens())
    assert got_token is not None
    assert got_token.access_token == "at-1"  # noqa: S105
    assert got_token.refresh_token == "rt-1"  # noqa: S105

    got_info = asyncio.run(storage.get_client_info())
    assert got_info is not None
    assert got_info.client_id == "abc"


def test_token_storage_empty_when_absent(unit_home: Path) -> None:
    storage = oauth_mod._FileTokenStorage("none")
    assert asyncio.run(storage.get_tokens()) is None
    assert asyncio.run(storage.get_client_info()) is None


def test_token_storage_file_permissions(unit_home: Path) -> None:
    """Tokens are a secret: the file must be owner-only."""
    storage = oauth_mod._FileTokenStorage("exa")
    token = OAuthToken(
        access_token="at",  # noqa: S106
        token_type="Bearer",  # noqa: S106
        expires_in=60,
    )
    asyncio.run(storage.set_tokens(token))
    # Created 0600 from the start (os.open mode), not write-then-chmod — the
    # mode must never depend on umask (audit round-2 security P3).
    assert (storage._path.stat().st_mode & 0o777) == 0o600


# ─── CallbackServer ───────────────────────────────────────────────────────


async def test_callback_server_captures_code() -> None:
    """A real HTTP request to the loopback redirect URI yields the code/state/iss."""
    cb = oauth_mod._CallbackServer(port=8932)
    task = asyncio.create_task(cb.wait_for_callback())

    # Give the server a moment to bind, then hit the redirect URI. (No socket
    # probe — a stray connect would be eaten by the handler as a non-callback.)
    await asyncio.sleep(0.3)

    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "http://127.0.0.1:8932/callback?code=the-code&state=st&iss=https%3A%2F%2Fissuer",
            timeout=5.0,
        )
    assert resp.status_code == 200

    result = await asyncio.wait_for(task, timeout=5)
    assert result.code == "the-code"
    assert result.state == "st"
    assert result.iss == "https://issuer"


async def test_callback_server_redirect_uri() -> None:
    assert oauth_mod._CallbackServer().redirect_uri == (
        f"http://127.0.0.1:{oauth_mod._OAUTH_CALLBACK_PORT}/callback"
    )


# ─── issuer normalization shim ────────────────────────────────────────────


def test_normalize_issuer_strips_trailing_slash() -> None:
    assert oauth_mod._normalize_issuer("https://accounts.google.com/") == (
        "https://accounts.google.com"
    )
    assert oauth_mod._normalize_issuer("https://accounts.google.com") == (
        "https://accounts.google.com"
    )


def test_normalized_validate_issuer_accepts_google_trailing_slash() -> None:
    """Google's two metadata docs disagree on the trailing slash; the shim
    treats them as the same issuer (RFC 3986 §6.2.1)."""
    from mcp.shared.auth import OAuthMetadata
    from pydantic import AnyHttpUrl

    md = OAuthMetadata(
        issuer=AnyHttpUrl("https://accounts.google.com/"),
        authorization_endpoint=AnyHttpUrl("https://accounts.google.com/o/oauth2/v2/auth"),
        token_endpoint=AnyHttpUrl("https://oauth2.googleapis.com/token"),
    )
    oauth_mod._normalized_validate_issuer(md, "https://accounts.google.com")  # no raise


def test_normalized_validate_issuer_still_rejects_mismatch() -> None:
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthMetadata
    from pydantic import AnyHttpUrl

    md = OAuthMetadata(
        issuer=AnyHttpUrl("https://evil.example.com"),
        authorization_endpoint=AnyHttpUrl("https://evil.example.com/auth"),
        token_endpoint=AnyHttpUrl("https://evil.example.com/token"),
    )
    with pytest.raises(OAuthFlowError, match="issuer mismatch"):
        oauth_mod._normalized_validate_issuer(md, "https://accounts.google.com")
