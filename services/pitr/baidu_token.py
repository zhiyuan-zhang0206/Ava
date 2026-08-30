"""Baidu Netdisk OAuth token manager for the PITR store backend.

Implements the ``StoreTokenManager`` skeleton from
:mod:`services.pitr.token_manager`: one writer refreshes the pair
(the uploader daemon is single-worker; a file lock guards any extra
actor), the replacement pair is persisted atomically with mode 0600,
and ``health()`` exposes the access token's remaining lifetime for the
daemon health payload.

Baidu specifics (official docs): the access token lives 30 days; the
refresh token is one-time-use — every refresh response REPLACES it, and
a refresh failure (or racing two refreshes) invalidates the stored pair
until the operator re-authorizes. A refresh failure is therefore a
permanent condition that health surfaces, never a silent retry loop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from services.pitr.token_manager import (
    TokenHealth,
    TokenState,
    read_token_state,
    write_token_state,
)

TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"  # noqa: S105 — public endpoint URL
"""Authorization-code-mode token endpoint."""

ACCESS_TTL_SECONDS = 30 * 24 * 3600
"""Official access-token lifetime (30 days); refresh early to stay safe."""

_REFRESH_MARGIN_SECONDS = 24 * 3600
"""Refresh this far ahead of expiry so a transient failure never strands
the daemon on a dead token."""


class BaiduTokenError(RuntimeError):
    """The token pair cannot be refreshed without operator action."""


class BaiduCredentials:
    """One app identity: appKey + secretKey + the durable refresh token."""

    def __init__(self, path: Path) -> None:
        try:
            raw: object = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise BaiduTokenError(f"invalid Baidu credential file: {path.name}") from exc
        if not isinstance(raw, dict):
            raise BaiduTokenError(f"invalid Baidu credential file: {path.name}")
        payload = cast(dict[str, object], raw)
        try:
            self.app_key = str(payload["app_key"])
            self.secret_key = str(payload["secret_key"])
            self.refresh_token = str(payload["refresh_token"])
        except KeyError as exc:
            raise BaiduTokenError(f"Baidu credential file is missing {exc.args[0]}") from None
        if not self.app_key or not self.secret_key or not self.refresh_token:
            raise BaiduTokenError("Baidu credential file holds an empty credential")


class BaiduTokenManager:
    """Single-writer OAuth lifecycle for one Baidu app identity."""

    def __init__(self, credentials: BaiduCredentials, state_path: Path) -> None:
        self._credentials = credentials
        self._state_path = state_path
        self._refresh_error: str | None = None

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        state = self._load_state()
        now = datetime.now(UTC)
        if (
            not force_refresh
            and state is not None
            and state.remaining_seconds(now) > _REFRESH_MARGIN_SECONDS
        ):
            return state.access_token
        state = self._refresh(state)
        return state.access_token

    def _load_state(self) -> TokenState | None:
        try:
            return read_token_state(self._state_path)
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError) as exc:
            raise BaiduTokenError(f"stored Baidu token state is unreadable: {exc}") from exc

    def _refresh(self, _previous: TokenState | None) -> TokenState:
        """One refresh at a time: a file lock guards the OAuth exchange
        against a second writer, because Baidu's refresh token is
        single-use and a losing race would burn the only valid pair."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._state_path.with_suffix(".lock")
        import fcntl  # POSIX-only; imported lazily so the module imports on Windows

        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            # Re-read under the lock: another writer may have just refreshed.
            state = self._load_state()
            now = datetime.now(UTC)
            if state is not None and state.remaining_seconds(now) > _REFRESH_MARGIN_SECONDS:
                return state
            refresh_token = (
                self._credentials.refresh_token
                if state is None
                else state.refresh_token or self._credentials.refresh_token
            )
            try:
                response = httpx.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._credentials.app_key,
                        "client_secret": self._credentials.secret_key,
                    },
                    timeout=30,
                )
            except httpx.HTTPError as exc:
                self._refresh_error = str(exc)
                raise BaiduTokenError(f"Baidu token refresh transport failed: {exc}") from exc
        if response.status_code != 200:
            self._refresh_error = f"HTTP {response.status_code}"
            raise BaiduTokenError(f"Baidu token refresh rejected with HTTP {response.status_code}")
        try:
            raw: object = response.json()
        except ValueError as exc:
            raise BaiduTokenError("Baidu token refresh returned a non-JSON payload") from exc
        if not isinstance(raw, dict):
            raise BaiduTokenError("Baidu token refresh returned a non-object payload")
        payload = cast(dict[str, Any], raw)
        if "access_token" not in payload or "expires_in" not in payload:
            raise BaiduTokenError(f"Baidu token refresh omitted required fields: {sorted(payload)}")
        try:
            expires_in = int(payload["expires_in"])
        except (TypeError, ValueError) as exc:
            raise BaiduTokenError("Baidu token refresh carried an invalid expires_in") from exc
        if expires_in <= 0:
            raise BaiduTokenError("Baidu token refresh carried a non-positive expires_in")
        new_state = TokenState(
            access_token=str(payload["access_token"]),
            refresh_token=(
                None if payload.get("refresh_token") is None else str(payload["refresh_token"])
            ),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )
        write_token_state(self._state_path, new_state)
        self._refresh_error = None
        return new_state

    def health(self) -> TokenHealth:
        try:
            state = self._load_state()
        except BaiduTokenError as exc:
            return TokenHealth(
                remaining_seconds=None,
                expires_at=None,
                last_refresh_at=None,
                refresh_error=str(exc),
            )
        if state is None:
            return TokenHealth(
                remaining_seconds=None,
                expires_at=None,
                last_refresh_at=None,
                refresh_error=self._refresh_error,
            )
        return TokenHealth(
            remaining_seconds=state.remaining_seconds(datetime.now(UTC)),
            expires_at=state.expires_at.isoformat(),
            last_refresh_at=None,
            refresh_error=self._refresh_error,
        )
