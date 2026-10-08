"""Configuration slice of the insights service.

Fields keep their flat registry names. `services/derived/insights/daemon.py` (the composition
root) is the only module of the package that reads `settings`; it builds this slice and
hands it to the app. See `future/infra/security/dependency-injection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InsightsConfig:
    # `display.run_timeline_message_text_max`: characters of one raw message part served before it is clipped.
    run_timeline_message_text_max: int
