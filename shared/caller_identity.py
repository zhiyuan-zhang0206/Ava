"""Asserted caller provenance, never an authentication or authorization principal.

This is reader support, not permission to emit to old consumers. Producers must
wait for consumer convergence. No caller-supplied field grants authority.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_PART = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
PREFIXES = ("external_agent:", "unknown:")


class CallerIdentity(BaseModel):
    """A bounded, self-asserted identity; credentials remain server-owned facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["external_agent", "unknown"]
    subject: str = Field(min_length=1, max_length=20, pattern=_PART)
    instance: str | None = Field(default=None, min_length=1, max_length=14, pattern=_PART)

    def source(self) -> str:
        """Return the display projection, understood only by v1 consumers."""
        suffix = f":{self.instance}" if self.instance is not None else ""
        return f"{self.kind}:{self.subject}{suffix}"

    @classmethod
    def from_source(cls, source: str) -> CallerIdentity:
        """Decode only our explicit namespace, rejecting malformed identities."""
        if not source.startswith(PREFIXES):
            raise ValueError("not a caller identity source")
        parts = source.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("caller source requires kind:subject[:instance]")
        return cls.model_validate(
            {
                "kind": parts[0],
                "subject": parts[1],
                "instance": parts[2] if len(parts) == 3 else None,
            }
        )
