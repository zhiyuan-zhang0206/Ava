"""Configuration slice of the page-server daemon.

Fields keep their flat registry names. `services/page_server/daemon.py` builds the slice
(`page_server_config()`, the composition root) and threads it through the reconcile pass.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageServerConfig:
    page_server_poll_interval_seconds: float
    # The live-event channel PageClosed is published on (a field the event bus shares).
    events_channel: str
    # The dead-show-page scan runs once per heartbeat interval.
    heartbeat_interval_seconds: float
