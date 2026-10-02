"""The labeler slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.labeler import daemon
from services.labeler.config import LabelerConfig


def labeler_config(**overrides: Any) -> LabelerConfig:
    return replace(daemon.labeler_config(), **overrides)
