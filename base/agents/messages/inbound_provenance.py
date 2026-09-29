"""Server-owned provenance facts persisted beside gateway inbounds.

These facts are audit evidence, never an authorization decision. In particular,
a mismatch between an asserted ``agent:N`` source and a verified
``agent_token:M`` credential is recorded as ``False`` and delivery proceeds.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_AGENT_SOURCE = re.compile(r"^agent:([1-9][0-9]*)$")
_AGENT_TOKEN = re.compile(r"^agent_token:([1-9][0-9]*)$")
SOURCE_VERIFIED_BY_MAX_LENGTH = 120
SOURCE_TRANSPORT_MAX_LENGTH = 80
CONTENT_HASH_LENGTH = 64


@dataclass(frozen=True, slots=True)
class InboundProvenance:
    """Credential and transport facts established by the server boundary."""

    source_verified_by: str | None
    source_transport: str

    def __post_init__(self) -> None:
        if (
            self.source_verified_by is not None
            and len(self.source_verified_by) > SOURCE_VERIFIED_BY_MAX_LENGTH
        ):
            raise ValueError("source_verified_by exceeds the 120-character persistence limit")
        if len(self.source_transport) > SOURCE_TRANSPORT_MAX_LENGTH:
            raise ValueError("source_transport exceeds the 80-character persistence limit")


def source_assertion_match(source: str, provenance: InboundProvenance) -> bool | None:
    """Compare only a complete agent source/token pair; unknown stays NULL."""
    source_match = _AGENT_SOURCE.fullmatch(source)
    if source_match is None or provenance.source_verified_by is None:
        return None
    token_match = _AGENT_TOKEN.fullmatch(provenance.source_verified_by)
    if token_match is None:
        return None
    return int(source_match.group(1)) == int(token_match.group(1))


def content_sha256(content: str) -> str:
    """Return the lowercase SHA-256 digest of the exact persisted text."""
    digest = hashlib.sha256(content.encode()).hexdigest()
    if len(digest) != CONTENT_HASH_LENGTH:
        raise RuntimeError("SHA-256 produced a digest with an invalid length")
    return digest
