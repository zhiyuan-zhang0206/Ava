"""Controls for one explicitly observed active turn."""

from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from ava.sdk_surface.validation import coerce_typed
from base.agents.incarnation.native_work_models import NativeCancelAcceptance, NativeWorkTarget
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, validate_idempotency_key

__all_for_ava__ = ["cancel", "observe"]


def observe(agent_id: int) -> NativeWorkTarget:
    """Observe an active turn to target a cancellation.

    Fails for inactive or unsupported work. Keep the returned target with the
    cancellation's key; observing again may describe another turn.
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    if not 0 < agent_id < 2**63:
        raise ValueError("work observation requires a positive agent ID")
    agent_identity.validate_external_identity()
    response = transport.get(f"/api/keyed/v1/agents/{agent_id}/native-work")
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("active work observation is unconfirmed")
    target = NativeWorkTarget.model_validate(response.json())
    if target.agent_id != agent_id:
        raise ValueError("active work observation targets another agent")
    return target


def cancel(target: NativeWorkTarget, *, idempotency_key: str) -> NativeCancelAcceptance:
    """Request cancellation only for the observed turn.

    Reuse the same key and target after an uncertain result. Acceptance may
    precede the actual stop; this request cannot cancel a later turn.
    """
    target = coerce_typed(target, "target", NativeWorkTarget)
    # Revalidate constructed/copied instances at the raw request boundary.
    target = NativeWorkTarget.model_validate(target.model_dump(mode="json"))
    key = validate_idempotency_key(idempotency_key)
    agent_identity.validate_external_identity()
    response = transport.post(
        f"/api/keyed/v1/agents/{target.agent_id}/cancel-work",
        target.model_dump(mode="json"),
        idempotency_key=key,
        idempotency_scope=PRINCIPAL_SCOPE,
    )
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("work cancellation acceptance is unconfirmed")
    accepted = NativeCancelAcceptance.model_validate(response.json())
    if accepted.target != target:
        raise ValueError("work cancellation acceptance targets another turn")
    return accepted
