"""SDK admission and immutable receipt validation for launch retries."""

from typing import cast
from uuid import UUID

from ava.gateway_client import transport
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, validate_idempotency_key
from ops.rpc_schemas.launch_retry import RetryLaunchAccepted, RetryLaunchRequest


def get_launch_attempt(agent_id: int) -> UUID:
    """Read the current metadata attempt; an absent old field is not evidence."""
    if type(agent_id) is not int or not 0 < agent_id < 2**63:
        raise ValueError("launch attempt observation requires a positive agent ID")
    response = transport.get(f"/api/agents/{agent_id}")
    transport.raise_from_response(response)
    raw: object = response.json()
    if not isinstance(raw, dict):
        raise TypeError("launch attempt observation is unavailable")
    values = cast("dict[str, object]", raw)
    attempt = values.get("last_launch_attempt_id")
    if not isinstance(attempt, str):
        raise TypeError("launch attempt observation is unavailable")
    return UUID(attempt)


def validate_retry_admission(*, key: str, prior: str | UUID) -> tuple[str, RetryLaunchRequest]:
    """Require a caller-owned key and observed attempt before HTTP."""
    key = validate_idempotency_key(key)
    if not isinstance(prior, (str, UUID)):
        raise TypeError("expected_prior_attempt_id must be a UUID or UUID string")
    return key, RetryLaunchRequest(
        expected_prior_attempt_id=UUID(prior) if isinstance(prior, str) else prior
    )


def retry_launch(
    agent_id: int,
    *,
    idempotency_key: str,
    expected_prior_attempt_id: str | UUID,
) -> int:
    """Retry one observed attempt through the fixed guarded operation."""
    key, request = validate_retry_admission(
        key=idempotency_key,
        prior=expected_prior_attempt_id,
    )
    if type(agent_id) is not int or not 0 < agent_id < 2**63:
        raise ValueError("guarded launch retry requires a positive agent ID")
    response = transport.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        request.model_dump(mode="json"),
        idempotency_key=key,
        idempotency_scope=PRINCIPAL_SCOPE,
    )
    transport.raise_from_response(response)
    raw: object = response.json()
    if not isinstance(raw, dict):
        raise TypeError("launch retry acceptance is unconfirmed")
    values = cast("dict[str, object]", raw)
    if (
        response.status_code != 200
        or type(values.get("agent_id")) is not int
        or values.get("accepted") is not True
        or values.get("execution_observed") is not False
    ):
        raise ValueError("launch retry acceptance is unconfirmed")
    accepted = RetryLaunchAccepted.model_validate(values)
    if (
        accepted.agent_id != agent_id
        or accepted.prior_attempt_id != request.expected_prior_attempt_id
        or accepted.launch_attempt_id == accepted.prior_attempt_id
    ):
        raise ValueError("launch retry acceptance is unconfirmed")
    return accepted.agent_id
