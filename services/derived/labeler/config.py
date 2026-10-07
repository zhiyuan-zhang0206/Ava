"""Configuration slice of the labeler daemon.

Fields keep their flat registry names. `services/derived/labeler/daemon.py` (the composition
root) is the only module of the package that reads `settings`; it builds this slice
and hands it to the label generation. See `future/infra/security/dependency-injection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelerConfig:
    labeler_model: str
    labeler_max_chars: int
