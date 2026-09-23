"""The `POST /ops` request envelope -- one definition for every receiver.

The envelope's home is here, in `shared`, because two very different receivers
must agree on it byte-for-byte: the full ops daemon (which may import anything)
and the restricted bootstrap observer (which must never import the ops stack --
its startup refuses an interpreter that imported `shared.config`, and
`ops.rpc_schemas` pulls that in transitively). `ops.rpc_schemas` re-exports this
class so every existing importer keeps its import path.
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
    (spawn / cluster_update / lifecycle): every retry of one logical op carries
    the SAME key, and the ops server replays the first run's stored outcome
    instead of re-executing (services/agent_ops/daemon.py:_dispatch_idempotent),
    so a lost response cannot duplicate the effect. Absent (None) for
    idempotent ops and for version-skewed old callers -- no dedup then."""

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
