"""External takeover consent handshake (legacy import surface)."""

# Package door. This `__init__` is the consent handshake an agent calls
# (`ava.impersonation.accept` / `reject`); its docstring stays the one-liner the
# agent can see. The submodules serve the takeover machinery around it and are
# never agent-facing:
#   launch   the bootstrap message a launched coding process receives
#            (`ava.shell.coding_tools`, the use-other-agents skill)
# It is not imported here: `import ava` loads this module eagerly.

from typing import NoReturn

from ava import agent_identity
from ava.sdk_surface.validation import coerce_str
from base.agents.lifecycle import AgentImpersonation
from base.native_process.runtime_incarnation import RuntimeIncarnation, current_incarnation

# Existing in-flight consent requests may still call accept/reject by name.
# New sessions prepare automatically, so these are absent from normal discovery.
# The reply path (say) is CLI-only (task #3658) — nothing here is discovered.
__all_for_ava__ = []


def _native_incarnation() -> RuntimeIncarnation:
    agent_identity.assert_self_action("impersonation")
    incarnation = current_incarnation(agent_identity.require_agent_id())
    if incarnation is None:
        raise RuntimeError("impersonation acceptance requires the admitted native runtime")
    return incarnation


def accept(request_id: str, start_message: str) -> NoReturn:
    """Accept an external takeover request and end this code execution.

    Save your working state first. Write `start_message` as your briefing for
    the external session: the current work, the context it needs, what done
    looks like, and how to acknowledge incoming messages. The briefing is
    delivered to the external session when the takeover starts; an empty one
    is rejected. The external agent starts only after your execution resources
    have closed and your conversation is durably saved. Your native loop
    resumes after release or lease expiry, receiving the controller's summary
    as the session's end message.

    The takeover activates only after its bound inbox relay is live. If the
    relay cannot start, the acceptance rolls back loudly (the lease becomes
    rejected with the reason) and you keep running as native.
    """
    from base.agents.impersonation import accept as accept_request

    incarnation = _native_incarnation()
    accept_request(
        coerce_str(request_id, "request_id"),
        incarnation.agent_id,
        incarnation,
        coerce_str(start_message, "start_message"),
    )
    raise AgentImpersonation


def reject(request_id: str, reason: str = "") -> None:
    """Decline a takeover request; your current execution continues."""
    from base.agents.impersonation import reject as reject_request

    incarnation = _native_incarnation()
    reject_request(
        coerce_str(request_id, "request_id"),
        incarnation.agent_id,
        incarnation,
        coerce_str(reason, "reason"),
    )
