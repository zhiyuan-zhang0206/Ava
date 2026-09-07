"""Update compatibility entry points over the normal agent pause boundary.

Phase A drains each runner locally before migration. A timeout aborts the
rollout; it never grants permission to force-kill an agent.
"""

from __future__ import annotations

_UPDATE_MODES = ("smooth", "force", "none")


def _quiesce_timeout_s(mode: str) -> float:
    if mode not in _UPDATE_MODES:
        raise ValueError(f"unknown update mode: {mode}")
    from shared.config import settings

    return settings.gateway.update_quiesce_timeout_seconds


def _quiesce_all_agents(timeout_s: float) -> bool:
    """Verify the local drain after all remote runners acknowledged Phase A.

    Remote flush proof belongs to each runner's Phase-A acknowledgement. Row
    status alone cannot prove hosted checkpoint completion.
    """
    from ops.agent_pause import pause_agents

    pause_agents(timeout_s)
    return True


def _quiesce_local_agents(mode: str) -> bool:
    """Use the same durable boundary for standalone and Phase-B updates.

    Even mode='none' verifies the existing hold; it cannot bypass an incomplete
    Phase A. Explicit force affects the later resource stop, never schema drain.
    """
    return _quiesce_all_agents(_quiesce_timeout_s(mode))
