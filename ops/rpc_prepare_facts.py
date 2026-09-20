"""`cluster_prepare_facts` op payload/result — one unit's prepared-facts shipment.

Split out of `ops/rpc_schemas.py` at the file-size budget (the
`ops/rpc_terminate.py` pattern). The op runs on a unit's normal ops daemon
during a managed-writer rollout's preparation phase (task #4129, channel A):
the daemon spawns the candidate image's `cli.prepared_facts` entry with the
payload's explicit operation/image references and relays the produced shipment
unchanged.

The shipment is expected inventory only — never closure or permission: the
receipt keeps `closure: "unknown"`, and `previous_selector` rides along solely
so the coordinator can assemble the later hop projections. Every digest is
re-verified by the coordinator from the bytes it receives; nothing here is
trusted as a claim.

Both models embed strict evidence models (`RolloutIdentity`, `CandidateUnitPlan`),
so their wire copies parse in JSON mode (`model_validate_json`) — what JSON-native
strings/lists require.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from shared.managed_writer_barrier import Digest, RolloutIdentity
from shared.managed_writer_observation import ExpectedProcess
from shared.managed_writer_publication import CandidateUnitPlan, PublishedUnit


class ImageRef(BaseModel):
    """One sealed release image's reference tuple (candidate or recovery)."""

    model_config = ConfigDict(extra="forbid")

    artifact_digest: Digest
    manifest_digest: Digest
    schema_digest: Digest


class RestrictedHopMaterial(BaseModel):
    """Read-once restricted-hop material for one unit (task #4129 I4).

    The imported dead predecessor orchestrator's identity (the unit's updater
    handoff owner) and the restricted-A observer's live launch context (its
    canonical private path plus its exact bytes). Expected evidence only --
    the hop re-derives every binding locally (the ``ava-ops`` command line is
    compared argument by argument and the running observer must echo the
    context's challenge), so nothing here is authority; the coordinator needs
    the pair only to assemble the hop request at its single seal point.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    predecessor: ExpectedProcess
    recovery_context_path: str
    recovery_context: str


class PrepareFactsPayload(BaseModel):
    """`cluster_prepare_facts` payload: the explicit pre-stop context.

    The operation names the live rollout lease; the candidate and recovery
    references are the coordinator's sealed choice. The entry re-checks all of
    it locally (and refuses on any drift) — the payload is a request, never
    authority.
    """

    model_config = ConfigDict(extra="forbid")

    operation: RolloutIdentity
    candidate: ImageRef
    recovery: ImageRef


class PrepareFactsResult(BaseModel):
    """`cluster_prepare_facts` result: the unit's shipment, byte-faithful.

    ``receipt_json`` carries the sealed receipt's exact text (its file bytes
    are ASCII JSON); the coordinator re-hashes it and binds
    ``unit.prepared_receipt_digest`` to it. ``previous_selector`` is the
    current selector's exact text, or None when no selector exists yet.
    Version 2 adds ``hop_material``: the restricted-hop identities read once
    at preparation (task #4129 I4) -- shipping them here keeps the coordinator
    the single assembly point for the hop request, and a unit that cannot
    surface them refuses its whole shipment rather than degrade.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    unit: PublishedUnit
    receipt_json: str
    candidate: CandidateUnitPlan
    recovery: ImageRef
    previous_selector: str | None
    hop_material: RestrictedHopMaterial
