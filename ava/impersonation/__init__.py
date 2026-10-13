"""External takeover consent handshake."""

# Package door. This `__init__` is the consent handshake an agent calls
# (`ava.impersonation.accept` / `reject`); its docstring stays the one-liner the
# agent can see. `attach` is the entry a controller's own Python process uses to
# borrow the leased identity; it is not agent-facing. The submodules serve the
# takeover machinery around it and are never agent-facing:
#   attachment  the attachment `attach` opens (heavy; loaded on first use)
#   launch      the bootstrap message a launched coding process receives
#               (`ava.shell.coding_tools`, the use-other-agents skill)
# Neither is imported here: `import ava` loads this module eagerly.

from typing import Any, NoReturn

from ava.sdk_surface import agent_identity, process_context
from ava.sdk_surface.validation import coerce_str
from base.agents.lifecycle import AgentImpersonation
from base.native_process.runtime_incarnation import RuntimeIncarnation

# The consent request message names accept/reject directly, so they are absent
# from normal discovery.
# The reply path (say) is CLI-only (task #3658) — nothing here is discovered.
__all_for_ava__ = []


def _native_incarnation() -> RuntimeIncarnation:
    agent_identity.assert_self_action("impersonation")

    return process_context.current_context().require_original_incarnation(
        agent_identity.require_agent_id()
    )


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
    from ava.sdk_surface.settings import bus, database
    from base.agents.impersonation import accept as accept_request

    incarnation = _native_incarnation()
    accept_request(
        database(),
        bus(),
        coerce_str(request_id, "request_id"),
        incarnation.agent_id,
        incarnation,
        coerce_str(start_message, "start_message"),
        local_machine=process_context.current_context().require_machine().name(),
    )
    raise AgentImpersonation


def reject(request_id: str, reason: str = "") -> None:
    """Decline a takeover request; your current execution continues."""
    from ava.sdk_surface.settings import bus, database
    from base.agents.impersonation import reject as reject_request

    incarnation = _native_incarnation()
    reject_request(
        database(),
        bus(),
        coerce_str(request_id, "request_id"),
        incarnation.agent_id,
        incarnation,
        coerce_str(reason, "reason"),
    )


def attach(session_id: int | str, *, agent_id: int | None = None) -> Any:
    """Borrow an active, unexpired agent identity in this local Python process.

    Use the session's integer id together with its owning agent id, or the
    lease handle. Attaching needs no credential: the caller must descend from
    the session's recorded controller process tree, rechecked on every lease
    use. Returns a context manager (`flush`, `close`, `native_snapshot`) that
    loads the agent's saved configuration and plugin state; attaching never
    renews the lease.
    """
    from ava.impersonation.attachment import attach as open_attachment

    return open_attachment(session_id, agent_id=agent_id)
