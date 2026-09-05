"""Cooperatively hand your next turn to an external agent."""

from typing import NoReturn

from ava import _boot
from ava._sdk_validation import coerce_str
from shared.lifecycle import AgentImpersonation
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation

__all_for_ava__ = ["accept", "reject"]


def _native_incarnation() -> RuntimeIncarnation:
    _boot.assert_self_action("impersonation")
    incarnation = current_incarnation(_boot.require_agent_id())
    if incarnation is None:
        raise RuntimeError("impersonation acceptance requires the admitted native runtime")
    return incarnation


def accept(request_id: str) -> NoReturn:
    """Accept an external takeover request and end this code execution.

    Save your working state first. The external agent starts only after your
    execution resources have closed and your conversation is durably saved.
    Your native loop resumes after release or lease expiry.
    """
    from shared.impersonation import accept as accept_request

    incarnation = _native_incarnation()
    accept_request(coerce_str(request_id, "request_id"), incarnation.agent_id, incarnation)
    raise AgentImpersonation


def reject(request_id: str, reason: str = "") -> None:
    """Decline a takeover request; your current execution continues."""
    from shared.impersonation import reject as reject_request

    incarnation = _native_incarnation()
    reject_request(
        coerce_str(request_id, "request_id"),
        incarnation.agent_id,
        incarnation,
        coerce_str(reason, "reason"),
    )
