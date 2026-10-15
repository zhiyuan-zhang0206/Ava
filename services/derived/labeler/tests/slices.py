from __future__ import annotations

from dataclasses import replace
from typing import Any

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.labeler import daemon
from services.derived.labeler.config import LabelerConfig


def labeler_config(**overrides: Any) -> LabelerConfig:
    return replace(daemon.labeler_config(), **overrides)


def labeler_db(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)
