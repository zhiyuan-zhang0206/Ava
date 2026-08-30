"""OAuth token lifecycle skeleton for PITR store backends.

GCS authenticates with a long-lived service account; the Baidu Netdisk
backend (PR-B) needs an OAuth2 token pair instead: a 30-day access token
plus a one-time-use refresh token, refreshed by exactly one writer (the
uploader daemon is single-worker; a file lock guards any additional
actor) and persisted atomically with mode 0600. This module fixes the
contract and the persistence discipline so the backend adapter only has
to supply the OAuth exchange itself.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast


@dataclass(frozen=True)
class TokenState:
    """One durable token pair. ``expires_at`` is the access token's wall
    clock, timezone-aware."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.access_token:
            raise ValueError("token state access token must not be empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("token state expiry must be timezone-aware")

    def remaining_seconds(self, now: datetime) -> float:
        """Seconds until expiry; negative once the access token is dead."""
        if now.tzinfo is None:
            raise ValueError("token health clock must be timezone-aware")
        return (self.expires_at - now).total_seconds()


@dataclass(frozen=True)
class TokenHealth:
    """Backend-agnostic token health snapshot for the daemon health payload."""

    remaining_seconds: float | None
    """None when no token has ever been provisioned."""

    expires_at: str | None
    last_refresh_at: str | None
    refresh_error: str | None


class StoreTokenManager(Protocol):
    """Single-writer token lifecycle for a store backend.

    ``get_access_token`` must return a live token, refreshing (and
    persisting the replacement pair atomically) when the current one is
    expired or ``force_refresh`` is set. ``health`` must never raise —
    the health endpoint reads it on every probe.
    """

    def get_access_token(self, *, force_refresh: bool = False) -> str: ...

    def health(self) -> TokenHealth: ...


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_token_state(path: Path, state: TokenState) -> None:
    """Persist one token pair atomically: 0600 tempfile + fsync + replace.

    The refresh response replaces the old pair; a crash mid-write must
    leave either the old or the new pair, never a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(asdict(state), output, sort_keys=True, separators=(",", ":"), default=str)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)
        _fsync_dir(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def read_token_state(path: Path) -> TokenState:
    """Strict parse of a persisted token pair; any deviation raises."""
    raw: object = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise TypeError("token state must be an object")
    payload = cast(dict[str, object], raw)
    try:
        return TokenState(
            access_token=str(payload["access_token"]),
            refresh_token=(
                None if payload.get("refresh_token") is None else str(payload["refresh_token"])
            ),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])).astimezone(UTC),
        )
    except KeyError as exc:
        raise TypeError(f"token state is missing {exc.args[0]}") from None
