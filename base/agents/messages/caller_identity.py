"""Asserted external-agent provenance, never an authentication or authorization principal.

An external agent (Claude Code, Codex, ...) that messages an Ava agent declares
who it is as ``external_agent:<subject>[:<instance>]``. The declaration is
self-asserted: no caller-supplied field grants authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_PART = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
EXTERNAL_AGENT_PREFIX = "external_agent:"


class CallerIdentity(BaseModel):
    """A bounded, self-asserted identity; credentials remain server-owned facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["external_agent"]
    subject: str = Field(min_length=1, max_length=20, pattern=_PART)
    instance: str | None = Field(default=None, min_length=1, max_length=20, pattern=_PART)

    def source(self) -> str:
        """Return the ``source`` string that carries this identity."""
        suffix = f":{self.instance}" if self.instance is not None else ""
        return f"{self.kind}:{self.subject}{suffix}"

    @classmethod
    def from_source(cls, source: str) -> CallerIdentity:
        """Decode only our explicit namespace, rejecting malformed identities."""
        if not is_caller_source(source):
            raise ValueError("not a caller identity source")
        parts = source.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("caller source requires external_agent:<subject>[:<instance>]")
        return cls.model_validate(
            {
                "kind": parts[0],
                "subject": parts[1],
                "instance": parts[2] if len(parts) == 3 else None,
            }
        )


def is_caller_source(source: str) -> bool:
    """Whether `source` is in the external-agent namespace (well-formed or not)."""
    return source.startswith(EXTERNAL_AGENT_PREFIX)


def caller_payload(source: str, payload: Mapping[str, object] | None) -> dict[str, object] | None:
    """Persist asserted structured provenance without changing unrelated payload.

    The reserved caller_identity key cannot contradict the source, inject an
    authenticated principal, or annotate a non-external source as a known caller.
    Reading a source proves no credentials.
    """
    caller = CallerIdentity.from_source(source) if is_caller_source(source) else None
    if payload is not None and "caller_identity" in payload:
        supplied = CallerIdentity.model_validate(payload["caller_identity"])
        if supplied != caller:
            raise ValueError("caller_identity conflicts with source")
    if caller is None:
        return dict(payload) if payload is not None else None
    result = dict(payload) if payload is not None else {}
    result["caller_identity"] = caller.model_dump(mode="json", exclude_none=True)
    return result
