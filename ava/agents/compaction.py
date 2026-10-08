"""Explicit manual compaction of one retained, closed source history."""

from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from ava.sdk_surface.validation import coerce_typed
from base.agents.compaction.models import CompactAcceptance, CompactStatus, CompactTarget
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, validate_idempotency_key

__all_for_ava__ = ["observe", "status", "submit"]


def observe(agent_id: int) -> CompactTarget:
    """Observe one eligible closed source history for manual compaction.

    Active, unsupported or stale sources fail. Keep the returned target with
    the submission key; a later observation may describe different history.
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    if type(agent_id) is not int or not 0 < agent_id < 2**63:
        raise ValueError("compaction observation requires a positive agent ID")
    agent_identity.validate_external_identity()
    response = transport.get(f"/api/keyed/v1/agents/{agent_id}/compact-target")
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("compaction source observation is unconfirmed")
    target = CompactTarget.model_validate(response.json())
    if target.source.agent_id != agent_id:
        raise ValueError("compaction source observation targets another agent")
    return target


def submit(target: CompactTarget, *, idempotency_key: str) -> CompactAcceptance:
    """Accept manual compaction of the exact observed source history.

    Reuse the original target/key after uncertainty. Acceptance does not mean
    the summary was generated or applied; inspect status separately.
    """
    target = coerce_typed(target, "target", CompactTarget)
    target = CompactTarget.model_validate(target.model_dump(mode="python"))
    key = validate_idempotency_key(idempotency_key)
    agent_identity.validate_external_identity()
    response = transport.post(
        f"/api/keyed/v1/agents/{target.source.agent_id}/compact-history",
        target.model_dump(mode="json"),
        idempotency_key=key,
        idempotency_scope=PRINCIPAL_SCOPE,
    )
    transport.raise_from_response(response)
    if response.status_code != 202:
        raise ValueError("manual compaction acceptance is unconfirmed")
    accepted = CompactAcceptance.model_validate(response.json())
    if accepted.target != target:
        raise ValueError("manual compaction acceptance targets another source")
    return accepted


def status(accepted: CompactAcceptance) -> CompactStatus:
    """Inspect execution and application evidence for this original acceptance.

    An accepted command may remain unresolved. Application and release of
    its native continuation are separate facts in the returned status.
    """
    accepted = coerce_typed(accepted, "accepted", CompactAcceptance)
    accepted = CompactAcceptance.model_validate(accepted.model_dump(mode="python"))
    agent_identity.validate_external_identity()
    response = transport.get(
        f"/api/keyed/v1/agents/{accepted.target.source.agent_id}/compact-commands/{accepted.command_id}"
    )
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("manual compaction status is unconfirmed")
    current = CompactStatus.model_validate(response.json())
    if current.acceptance != accepted:
        raise ValueError("manual compaction status identifies another acceptance")
    return current
