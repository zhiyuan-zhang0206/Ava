"""Explicit guarded creation admission shared by SDK entry points."""

from base.api_contracts.idempotency import validate_idempotency_key


def validate_spawn_admission(
    *, require_idempotency: bool, key: str | None, fork_from: int | None
) -> str | None:
    """Reject unsupported strong requests before identity resolution or HTTP."""
    if not isinstance(require_idempotency, bool):
        raise TypeError("require_idempotency must be a bool")
    if key is not None:
        key = validate_idempotency_key(key)
    if require_idempotency:
        if key is None:
            raise ValueError("require_idempotency requires an explicit idempotency key")
        if fork_from is not None:
            raise ValueError("require_idempotency does not support fork_from")
    return key
