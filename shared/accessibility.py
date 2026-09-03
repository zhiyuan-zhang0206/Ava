"""Accessibility status carried from the converge that measures it to the agent.

The measurement itself is NOT here: the signed permissions helper is the
process that posts synthetic input and reads the accessibility tree, so its
Accessibility grant is the only one that matters. The helper reports that fact
through ``services.permissions_helper.client.check_accessibility``. A probe in
the calling process would instead report the inherited grant of whatever
started that process, which is a different fact entirely.

This module owns the result type and status file. The converge step writes the
file (``write_status``); an agent claims and clears it at startup (see
``agent.startup``) and notifies the user when accessibility is unavailable, so
the condition never stays silent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from shared.paths import ava_home

_STATUS_FILE = "accessibility_status.json"


class AccessibilityState(StrEnum):
    """What the helper's Accessibility probe learned.

    A helper that did not answer has an unread grant, not a missing one. The
    two unavailable states have different fixes, so they stay distinct.
    """

    GRANTED = "granted"
    NOT_GRANTED = "not_granted"
    HELPER_UNREACHABLE = "helper_unreachable"


_HEADLINES = {
    AccessibilityState.GRANTED: "Accessibility available",
    AccessibilityState.NOT_GRANTED: "Accessibility permission missing",
    AccessibilityState.HELPER_UNREACHABLE: "Permissions helper unreachable",
}


@dataclass
class AccessibilityStatus:
    state: AccessibilityState
    diagnostic: str = ""

    @property
    def available(self) -> bool:
        return self.state is AccessibilityState.GRANTED

    @property
    def headline(self) -> str:
        """Short label for this state -- notification title, converge log prefix."""
        return _HEADLINES[self.state]

    def to_json(self) -> str:
        return json.dumps({"state": self.state.value, "diagnostic": self.diagnostic})

    @classmethod
    def from_json(cls, text: str) -> AccessibilityStatus:
        data = json.loads(text)
        return cls(state=AccessibilityState(data["state"]), diagnostic=data["diagnostic"])

    @classmethod
    def from_file(cls, path: Path) -> AccessibilityStatus | None:
        if not path.exists():
            return None
        try:
            return cls.from_json(path.read_text())
        except Exception:
            # An interrupted write or obsolete shape is replaced by the next
            # converge, so one skipped notice is safer than inventing a fault.
            return None


def status_file_path() -> Path:
    """Path to the Accessibility status file under ``$AVA_HOME``."""
    return ava_home() / _STATUS_FILE


def write_status(status: AccessibilityStatus) -> None:
    """Write the Accessibility status to its well-known file under ``$AVA_HOME``."""
    status_file_path().write_text(status.to_json())


def read_status() -> AccessibilityStatus | None:
    """Read the current Accessibility status from disk, or None if absent."""
    return AccessibilityStatus.from_file(status_file_path())


def clear_status() -> None:
    """Remove the status file (e.g. after user has been notified)."""
    status_file_path().unlink(missing_ok=True)
