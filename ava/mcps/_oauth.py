"""OAuth 2.1 (authorization code + PKCE) support for remote MCP servers.

The MCP daemon connects to hosted Streamable HTTP servers. Servers that
require OAuth (Google-flavoured authorization-code flows, RFC 8414 resource
metadata discovery) get an `httpx2.Auth` provider built here: on a 401 the
provider discovers the authorization server, registers this client (RFC
7591), opens the user's browser at the authorization URL, receives the code
on a loopback callback, exchanges it for tokens and stores them — and
refreshes them transparently afterwards.

Browser session note: the daemon runs on the user's headed machine, so the
authorization URL opens in the user's Chrome (already logged into Google
etc.) — the flow is exactly "log in with Google" in a normal browser.

Concurrency: one in-flight authorization per server, guarded by an
asyncio.Lock. While a flow is pending, other connections for that server
wait on the lock; when it finishes (tokens stored), they proceed with the
cached client. A failed flow releases the lock and surfaces the error.

Tokens persist per server at `$AVA_HOME/mcp_oauth/<server>.json` (0600),
so authorization happens once per server, not per daemon restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from contextlib import suppress
from typing import Any

from loguru import logger
from pydantic import AnyUrl

from shared.paths import ava_home

# Loopback callback port for the authorization-code redirect. It is part of the
# registered redirect_uris (OAuth requires exact-match redirect URIs), so it is
# a fixed constant, not a random port: the client metadata is minted once per
# server and reused (persisted with the tokens). If the port is taken, fail
# fast rather than register a redirect URI the callback can never receive.
_OAUTH_CALLBACK_PORT = 8931
_OAUTH_CALLBACK_PATH = "/callback"

# How long an authorization flow may take (the user has to click through a
# browser). Longer than the generic connect timeout on purpose: an in-flight
# authorization must not be cut off by the request path's own timeout.
_OAUTH_FLOW_TIMEOUT_S = 600.0

# OAuth client identity registered with authorization servers (RFC 7591).
_CLIENT_NAME = "Ava MCP Daemon"


class _FileTokenStorage:
    """Persist OAuth tokens + registered client info per server in `$AVA_HOME/mcp_oauth/`."""

    def __init__(self, server: str) -> None:
        self._path = ava_home() / "mcp_oauth" / f"{server}.json"

    def _read(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        # Created 0600 from the start (audit round-2 security P3): these files
        # hold OAuth tokens, and a write-then-chmod leaves a 0644 window under
        # a permissive umask. os.open's mode is subject to umask, which can
        # only remove bits — the result is always 0600 or stricter.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        data = self._read().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: Any) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json") if tokens is not None else None
        self._write(data)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: Any) -> None:
        data = self._read()
        data["client_info"] = (
            client_info.model_dump(mode="json") if client_info is not None else None
        )
        self._write(data)


class _CallbackServer:
    """One-shot loopback HTTP server that captures the authorization redirect.

    Binds 127.0.0.1 only. Serves the redirect URI once, parses
    code/state/iss out of the query, shows a "you can close this tab" page,
    then shuts down.
    """

    def __init__(self, port: int = _OAUTH_CALLBACK_PORT) -> None:
        self.port = port
        self._result: asyncio.Future[Any] | None = None

    async def wait_for_callback(self) -> Any:
        """Start serving on the loopback port and await one callback.

        Raises:
            OSError: the port is already bound (another process owns it).
        """
        from mcp.client.auth import AuthorizationCodeResult

        self._result = asyncio.get_running_loop().create_future()

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await reader.readline()
                if not line:
                    # EOF without a request line (health probe / stray connect) —
                    # not an authorization callback; ignore it.
                    writer.close()
                    return
                parts = line.decode("utf-8", "replace").split(" ")
                path = parts[1] if len(parts) > 1 else "/"
                # Drain the rest of the request so the client sees a complete response.
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                body = (
                    b"<html><body><h2>Authorization complete</h2>"
                    b"<p>You can close this tab and return to Ava.</p></body></html>"
                )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()

                from urllib.parse import parse_qs, urlparse

                from mcp.client.auth import OAuthFlowError

                fut = self._result
                if fut is None:
                    return
                query = parse_qs(urlparse(path).query)
                code = query.get("code", [None])[0]
                if not code:
                    fut.set_exception(OAuthFlowError("authorization callback missing 'code'"))
                    return
                fut.set_result(
                    AuthorizationCodeResult(
                        code=code,
                        state=query.get("state", [None])[0],
                        iss=query.get("iss", [None])[0],
                    )
                )
            except Exception as e:
                fut = self._result
                if fut is not None and not fut.done():
                    fut.set_exception(e)

        server = await asyncio.start_server(_handle, "127.0.0.1", self.port)
        try:
            return await asyncio.wait_for(self._result, timeout=_OAUTH_FLOW_TIMEOUT_S)
        finally:
            server.close()
            with suppress(Exception):
                await server.wait_closed()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{_OAUTH_CALLBACK_PATH}"


# One in-flight authorization per server (the daemon is single-process, so
# module state is daemon state). Other connections wait on the lock while a
# flow runs; after it completes the tokens are stored and they proceed.
_oauth_locks: dict[str, asyncio.Lock] = {}


async def _open_browser(auth_url: str) -> None:
    """Open the authorization URL in the user's browser (their logged-in Chrome).

    Falls back to logging the URL when no browser can be launched — the URL is
    still printed so a headless operator can paste it manually.
    """
    logger.info(f"[mcp-oauth] open authorization URL: {auth_url}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        logger.warning("[mcp-oauth] could not open a browser; authorize at: " + auth_url)


def _normalize_issuer(url: str) -> str:
    """Strip a trailing slash for issuer comparison (RFC 3986 §6.2.1).

    Google's protected-resource metadata advertises the authorization server
    as `https://accounts.google.com` while its AS metadata declares issuer
    `https://accounts.google.com/` — the SDK's strict string compare
    (validate_metadata_issuer) rejects that as a mismatch. Normalizing both
    sides is the spec-sanctioned comparison and fixes Google without relaxing
    anything for other issuers.
    """
    return url.rstrip("/")


def _install_issuer_normalization() -> None:
    """Monkeypatch the SDK's issuer comparison with a normalization shim.

    The SDK compares `str(metadata.issuer) != expected_issuer` verbatim
    (mcp.client.auth.utils.validate_metadata_issuer), which Google trips over
    (trailing-slash difference between its two metadata documents). We patch
    the function the SDK's oauth flow actually calls (it binds the import at
    module load), replacing the strict compare with a normalized one. Idempotent:
    patching twice is a no-op. Tracked upstream: modelcontextprotocol/python-sdk
    (2026-08-05, Google issuer trailing slash).
    """
    import mcp.client.auth.oauth2 as _oauth2
    import mcp.client.auth.utils as _utils

    _utils.validate_metadata_issuer = _normalized_validate_issuer  # type: ignore[attr-defined]
    _oauth2.validate_metadata_issuer = _normalized_validate_issuer  # type: ignore[attr-defined]


def _normalized_validate_issuer(oauth_metadata: Any, expected_issuer: str) -> None:
    from mcp.client.auth.exceptions import OAuthFlowError

    if _normalize_issuer(str(oauth_metadata.issuer)) != _normalize_issuer(expected_issuer):
        raise OAuthFlowError(
            f"Authorization server metadata issuer mismatch: "
            f"{oauth_metadata.issuer} != {expected_issuer}"
        )


def _install_token_auth_basic_shim() -> None:
    """Drop the duplicate body ``client_id`` on client_secret_basic requests.

    The SDK's ``OAuthContext.prepare_token_auth`` leaves the form body's
    ``client_id`` in place when the registered auth method is
    ``client_secret_basic`` (only ``client_secret`` is stripped; both the
    authorization-code exchange and the refresh request seed ``client_id``
    into the body). RFC 6749 §2.3.1 puts the client identity entirely in the
    Basic header for that method, and Notion's token endpoint rejects the
    duplicate outright (HTTP 400). Notion's /register endpoint returns
    ``client_secret_basic`` + a real secret no matter what was requested, so
    every Ava->Notion flow reaches this path. Tracked upstream:
    modelcontextprotocol/python-sdk (2026-08-30, prepare_token_auth — still
    unfixed on main as of 2026-08-30). Round-2 of the same class of fix as
    ``_install_issuer_normalization``; patching is idempotent in effect.
    """
    import mcp.client.auth.oauth2 as _oauth2

    original = _oauth2.OAuthContext.prepare_token_auth

    def _patched(
        self: Any, data: dict[str, str], headers: dict[str, str] | None = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        data, headers = original(self, data, headers)
        info = self.client_info
        if (
            info is not None
            and getattr(info, "token_endpoint_auth_method", None) == "client_secret_basic"
            and getattr(info, "client_secret", None)
        ):
            data.pop("client_id", None)
        return data, headers

    _oauth2.OAuthContext.prepare_token_auth = _patched  # type: ignore[attr-defined]


async def oauth_http_client(url: str, server: str) -> Any:
    """Build an httpx2.AsyncClient whose auth is the MCP OAuth provider.

    Serialized per server: while one connection is running the authorization
    flow, concurrent connections for the same server wait on the lock (their
    own 401 will reuse the tokens once stored). Returns the client; the caller
    owns its lifecycle.

    Raises:
        OSError: the loopback callback port is unavailable.
        OAuthFlowError / OAuthTokenError: the flow failed (user denied,
            endpoint misbehaved, token exchange failed).
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import (
        create_mcp_http_client,  # pyright: ignore[reportPrivateImportUsage] — re-exported from mcp.shared._httpx_utils
    )
    from mcp.shared.auth import OAuthClientMetadata

    _install_issuer_normalization()
    _install_token_auth_basic_shim()
    lock = _oauth_locks.setdefault(server, asyncio.Lock())
    async with lock:
        storage = _FileTokenStorage(server)
        callback = _CallbackServer()

        async def _redirect_handler(auth_url: str) -> None:
            await _open_browser(auth_url)

        async def _callback_handler() -> Any:
            return await callback.wait_for_callback()

        provider = OAuthClientProvider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name=_CLIENT_NAME,
                redirect_uris=[AnyUrl(callback.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=storage,
            redirect_handler=_redirect_handler,
            callback_handler=_callback_handler,
        )
        return create_mcp_http_client(auth=provider)
