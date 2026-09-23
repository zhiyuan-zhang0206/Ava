"""Screen-capture status carried from the converge that measures it to the agent.

The measurement itself is NOT here: OS-level screen capture is performed by the
signed permissions helper, so the only Screen Recording grant that matters is the
helper's, and the only way to read it is to ask the helper
(``services.permissions_helper.client.check_screen_capture``). A preflight inside the
calling process would report the grant of whatever started that process -- a
terminal session started over SSH holds none -- which is a different fact entirely.

This module owns the result type and the status file. The converge step writes
the file (``write_status``); an agent claims and clears it at startup (see
``agent.startup``) and, when capture is not available, notifies the user so the
condition never stays silent. The agent-facing notification lives in the agent
layer, which is the one allowed to reach the SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from shared.paths import ava_home

_STATUS_FILE = "screen_capture_status.json"


class ScreenCaptureState(StrEnum):
    """What the probe learned. "Could not tell" is not "no permission".

    Both non-available states break OS-level capture, but they are different
    faults with different fixes -- a grant is flipped in System Settings, a dead
    helper is a launchd problem -- so they are never collapsed into one bit.
    """

    AVAILABLE = "available"  # the helper answered and holds the Screen Recording grant
    NO_GRANT = "no_grant"  # the helper answered and does NOT hold the grant
    HELPER_UNREACHABLE = "helper_unreachable"  # no answer, so the grant is unknown


_HEADLINES = {
    ScreenCaptureState.AVAILABLE: "Screen capture available",
    ScreenCaptureState.NO_GRANT: "Screen Recording permission missing",
    ScreenCaptureState.HELPER_UNREACHABLE: "Permissions helper unreachable",
}


@dataclass
class ScreenCaptureStatus:
    state: ScreenCaptureState
    diagnostic: str = ""  # operator-facing explanation + fix; empty when available

    @property
    def available(self) -> bool:
        return self.state is ScreenCaptureState.AVAILABLE

    @property
    def headline(self) -> str:
        """Short label for this state -- notification title, converge log prefix."""
        return _HEADLINES[self.state]

    def to_json(self) -> str:
        return json.dumps({"state": self.state.value, "diagnostic": self.diagnostic})

    @classmethod
    def from_json(cls, text: str) -> ScreenCaptureStatus:
        data = json.loads(text)
        return cls(state=ScreenCaptureState(data["state"]), diagnostic=data["diagnostic"])

    @classmethod
    def from_file(cls, path: Path) -> ScreenCaptureStatus | None:
        if not path.exists():
            return None
        try:
            return cls.from_json(path.read_text())
        except Exception:
            # Unreadable covers a truncated write and a file left by a build that
            # wrote a different shape; either way the next converge rewrites it,
            # so treating it as "nothing to report" loses at most one pass.
            return None


def status_file_path() -> Path:
    """Path to the screen capture status file under ``$AVA_HOME``."""
    return ava_home() / _STATUS_FILE


def write_status(status: ScreenCaptureStatus) -> None:
    """Write the screen capture status to the well-known file under ``$AVA_HOME``."""
    status_file_path().write_text(status.to_json())


def read_status() -> ScreenCaptureStatus | None:
    """Read the current screen capture status from disk, or None if absent."""
    return ScreenCaptureStatus.from_file(status_file_path())


def clear_status() -> None:
    """Remove the status file (e.g. after user has been notified)."""
    status_file_path().unlink(missing_ok=True)
