"""Stand-ins for the announce hints (`base.events.live.announce`), whose first argument is the
`EventBus` they publish on."""

from __future__ import annotations

from collections.abc import Callable

import pytest


def recording(into: list[int]) -> Callable[[object, int], None]:
    """An announce stand-in that records the agent id it is called with."""

    def announce(_bus: object, agent_id: int) -> None:
        into.append(agent_id)

    return announce


def patch_announcements(
    monkeypatch: pytest.MonkeyPatch, module: object, *, changed: list[int], updated: list[int]
) -> None:
    """Replace the impersonation-changed and agent-updated hints `module` imported with
    recorders of the agent ids they are called with."""
    monkeypatch.setattr(module, "publish_impersonation_changed_sync", recording(changed))
    monkeypatch.setattr(module, "publish_agent_updated_sync", recording(updated))
