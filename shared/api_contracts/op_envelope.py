"""The shared `POST /ops` request envelope.

The receiver validates the current operation vocabulary before maintenance
admission or idempotency writes. Unknown kinds receive a failed op response.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OpEnvelope(BaseModel):
    """`POST /ops` request envelope. `kind` stays a bare str (not the OpKind
    Literal) so an unknown kind from a version-skewed peer becomes a 'failed' op
    result -- the dispatch switch owns the kind vocabulary -- rather than an
    envelope-parse rejection.

    `idempotency_key` is the caller-supplied dedup key for non-idempotent ops
    (spawn / lifecycle): every retry of one logical op carries
    the SAME key, and the ops server replays the first run's stored outcome
    instead of re-executing (services/agent_ops/daemon.py:_dispatch_idempotent),
    so a lost response cannot duplicate the effect. Absent (None) for
    idempotent ops and for version-skewed old callers -- no dedup then."""

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
