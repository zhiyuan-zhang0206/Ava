"""PITR execution receipts; the activation record owns all business evidence.

These settings-free values bind one action to its startup inputs and native
stop custody. They cannot discover or adopt a process during recovery.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from base.native_process.evidence import ExpectedProcess
from cli.commands.data_plane.maintenance_stop import DataStop
from cli.release_transition.request import Digest, Record


class PitrSeal(Record):
    configuration_digest: Digest
    auto_conf_digest: Digest
    archive_settings_digest: Digest
    postgres: ExpectedProcess
    postgres_state: dict[str, str]
    activation_digest: Digest


class PitrProgress(Record):
    action: Literal["activate", "rollback"]
    generation: int = Field(ge=1)
    maintenance_at: AwareDatetime
    record_digest: Digest | None
    record_intent: tuple[Digest | None, Digest] | None = None
    seal: PitrSeal | None = None
    data_stop: DataStop | None = None
    decisions: tuple[dict[str, str | int], ...] = ()
