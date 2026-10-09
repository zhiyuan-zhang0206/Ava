"""Controls for one explicitly observed active turn."""

from ava.gateway_client import transport
from ava.sdk_surface import agent_identity
from ava.sdk_surface.validation import coerce_typed
from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartProgress,
    NativeRestartRequest,
)
from base.agents.incarnation.native_work_models import NativeCancelAcceptance, NativeWorkTarget
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, validate_idempotency_key

__all_for_ava__ = ["cancel", "observe", "restart", "restart_status"]


def observe(agent_id: int) -> NativeWorkTarget:
    """Observe an active turn for explicit cancellation or restart.

    Fails for inactive or unsupported work. Keep the returned target with the
    command's key; observing again may describe another turn.
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    if type(agent_id) is not int or not 0 < agent_id < 2**63:
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
    target = NativeWorkTarget.model_validate(target.model_dump(mode="python"))
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


def restart(
    target: NativeWorkTarget,
    *,
    idempotency_key: str,
    config_overlay: dict[str, object] | None = None,
) -> NativeRestartAcceptance:
    """Request restart of the exact observed active turn.

    Retain the same target, key and overlay for recovery. The returned receipt
    proves acceptance; inspect restart_status for original execution evidence.
    """
    target = coerce_typed(target, "target", NativeWorkTarget)
    target = NativeWorkTarget.model_validate(target.model_dump(mode="python"))
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    key = validate_idempotency_key(idempotency_key)
    agent_identity.validate_external_identity()
    body = NativeRestartRequest(
        target=target, source=agent_identity.default_actor(), config_overlay=config_overlay
    )
    response = transport.post(
        f"/api/keyed/v1/agents/{target.agent_id}/restart-work",
        body.model_dump(mode="json"),
        idempotency_key=key,
        idempotency_scope=PRINCIPAL_SCOPE,
    )
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("work restart acceptance is unconfirmed")
    accepted = NativeRestartAcceptance.model_validate(response.json())
    if accepted.target != target:
        raise ValueError("work restart acceptance targets another turn")
    return accepted


def restart_status(accepted: NativeRestartAcceptance) -> NativeRestartProgress:
    """Inspect retained execution facts for the original restart command.

    Acceptance, original ownership release and observed successor admission
    are separate outcomes; current agent status cannot substitute for them.
    """
    accepted = coerce_typed(accepted, "accepted", NativeRestartAcceptance)
    accepted = NativeRestartAcceptance.model_validate(accepted.model_dump(mode="python"))
    agent_identity.validate_external_identity()
    response = transport.get(
        f"/api/keyed/v1/agents/{accepted.target.agent_id}/restart-commands/{accepted.command_id}"
    )
    transport.raise_from_response(response)
    if response.status_code != 200:
        raise ValueError("work restart progress is unconfirmed")
    progress = NativeRestartProgress.model_validate(response.json())
    if progress.acceptance != accepted:
        raise ValueError("work restart progress identifies another acceptance")
    return progress
