"""One-shot process-environment protocol for a rollout's fresh start child.

The rollout parent survives a source checkout while ``ava start`` must execute
the new tree in a child. This marker proves that the surviving parent knows how
to adopt a credential transition written by that child. It is deliberately not
runtime configuration: the child consumes it once, and ordinary starts never see
it.
"""

from __future__ import annotations

from collections.abc import Mapping

from shared.process_env import consume_process_marker, inherited_process_env
from shared.process_env import update_process_env as _update_process_env

ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV = "AVA_ROLLOUT_PARENT_CREDENTIAL_HANDOFF"
# Protocol v1 means the parent can replay the frozen five-field transition
# payload in ``cli.commands._data_plane_admin_secrets._Transition``. A future
# incompatible journal must advertise a new value; a v1 parent then fails closed
# and the child defers credential mutation.
ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION = "v1"


def consume_parent_credential_handoff() -> bool:
    """Consume and return the rollout parent's one-shot adoption capability."""
    return consume_process_marker(
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
        armed_value=ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
    )


def child_process_env() -> dict[str, str]:
    """Copy the live process environment and arm the rollout child handoff."""
    return inherited_process_env(
        {ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV: ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION}
    )


def update_process_env(values: Mapping[str, str]) -> None:
    """Apply a credential transition to this process for subsequent clients."""
    _update_process_env(values)
