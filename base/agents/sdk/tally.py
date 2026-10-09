"""The execution owner's complete, unsampled public SDK-call counts."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class SdkCallTally:
    """Shared by calls and threads belonging to one explicit execution context."""

    _counts: dict[str, int] = field(default_factory=dict[str, int], init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def add(self, fn: str) -> None:
        """Count one admitted invocation, including a body that failed."""
        with self._lock:
            self._counts[fn] = self._counts.get(fn, 0) + 1

    def snapshot(self) -> dict[str, int]:
        """Copy completed call counts while other execution threads may still run."""
        with self._lock:
            return dict(self._counts)
