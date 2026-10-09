"""The actual resources retained by one hosted turn's composition root."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HostedTurnResources:
    """Actual unresolved domains held by one turn Task, never by the model cache."""

    unresolved: dict[Path, object | None] = field(default_factory=dict[Path, object | None])
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    completions: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def complete(self, request: Path, expected: object | None) -> bool:
        """Only the original resource owner may discharge its exact entry."""
        if request not in self.unresolved or self.unresolved[request] is not expected:
            return False
        del self.unresolved[request]
        self.changed.set()
        return True


def hosted_resources_settled(resources: HostedTurnResources | None) -> bool:
    """Whether this exact turn scope has discharged every registered domain."""
    return resources is None or not resources.unresolved
