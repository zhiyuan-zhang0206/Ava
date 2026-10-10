"""Explicit SDK attachment capture contracts, without importing receipt storage."""

from typing import Any, Protocol


class SdkCaptureAdmission(Protocol):
    """One call's retained receipt admission, released after its final event."""

    def capture(self, event: Any) -> Any:
        """Append an eligible event to this call's original receipt."""
        ...

    def capture_failed(self) -> None:
        """Fence the original receipt after an event could not be captured."""
        ...

    def release(self) -> None:
        """Release admitted work and seal only when its original owner drains."""
        ...


class SdkCaptureOwner(Protocol):
    """The exclusive attachment owning admission and direct local audit capture."""

    def admit_sdk_call(self) -> SdkCaptureAdmission | None:
        """Reject calls after close starts; otherwise retain their receipt admission."""
        ...

    def capture_local_event(self, event: Any) -> Any:
        """Capture a direct local audit with this attachment's original gate."""
        ...
