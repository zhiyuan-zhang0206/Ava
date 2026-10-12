"""Explicit SDK attachment capture contracts, without importing receipt storage."""

from typing import Any, Protocol

from base.agents.contract import SdkCaptureAdmission


class SdkCaptureOwner(Protocol):
    """The exclusive attachment owning admission and direct local audit capture."""

    def admit_sdk_call(self) -> SdkCaptureAdmission | None:
        """Reject calls after close starts; otherwise retain their receipt admission."""
        ...

    def capture_local_event(self, event: Any) -> Any:
        """Capture a direct local audit with this attachment's original gate."""
        ...
