from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelerConfig:
    labeler_model: str
    labeler_max_chars: int
