"""``cluster_prepare_dispatch`` op payload/result -- one unit's sealed-plan validation.

Split out of `ops/rpc_schemas.py` at the file-size budget (the
`ops/rpc_terminate.py` pattern). The op runs on a unit's normal ops daemon
during a managed-writer rollout's dispatch phase (task #4129, channel B): the
daemon writes the sealed plan into its private `run/` directory, spawns the
candidate image's `cli.prepared_plan` entry to validate it against the local
unit, and relays the acknowledgement. No-effect: nothing is stopped, started,
migrated or selected; a refusal is an answer, not an exception.

The payload's `artifact_digest` only selects which retained image's interpreter
runs the validation; the entry re-derives the binding itself from the plan
bytes (`prepare_operator_input`), so the field is a hint, never authority. The
acknowledgement echoes the digest of the exact plan bytes that validated and
the local unit's prepared-receipt digest -- both re-checked against the
coordinator's sealed dispatch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.managed_writer_barrier import Digest

PREPARED_PLAN_PREFIX = "prepared-plan-"


def prepared_plan_name(plan_digest: str) -> str:
    """The one file name a dispatched plan is written under (0600, content-named)."""
    return f"{PREPARED_PLAN_PREFIX}{plan_digest}.json"


class PrepareDispatchPayload(BaseModel):
    """``cluster_prepare_dispatch`` payload: the sealed plan and its target image."""

    model_config = ConfigDict(extra="forbid")

    plan_json: str
    artifact_digest: Digest


class DispatchValidation(BaseModel):
    """The candidate entry's validation output: digests of what it actually checked."""

    model_config = ConfigDict(extra="forbid")

    plan_digest: Digest
    receipt_digest: Digest


class PrepareDispatchResult(BaseModel):
    """``cluster_prepare_dispatch`` result: one unit's acknowledgement.

    Exactly one of the shapes: a passing acknowledgement carries the validated
    plan digest and the local prepared-receipt digest; a refusal carries the
    sanitized reason and no receipt digest. The coordinator re-checks both
    against its sealed dispatch before the gate opens.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    plan_digest: Digest
    receipt_digest: Digest | None = None
    refusal: str | None = None

    @model_validator(mode="after")
    def passing_ack_carries_its_receipt(self) -> PrepareDispatchResult:
        if self.refusal is None and self.receipt_digest is None:
            raise ValueError("a passing dispatch acknowledgement requires its receipt digest")
        return self
