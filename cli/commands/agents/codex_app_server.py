"""Compatibility imports for the shared impersonator host transport."""

from base.agents.impersonation.host_transport import (
    LIVE_SUBMIT_TIMEOUT_SECONDS,
    default_control_endpoint,
    default_control_socket,
    live_submit,
    require_control_endpoint,
)

__all__ = [
    "LIVE_SUBMIT_TIMEOUT_SECONDS",
    "default_control_endpoint",
    "default_control_socket",
    "live_submit",
    "require_control_endpoint",
]
