"""Prepared unit-inventory schemas that the retained updater recovery journals embed.

Nothing here observes anything: the release inventory producer and its native
process, session and launcher readers are gone. These strict models stay so
`base.deploy.updater.recovery` can still read a journal the retired updater
left. They grant no admission or startup authority.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from base.native_process.evidence import Digest, EvidenceModel, ExpectedProcess


class ExpectedSession(EvidenceModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    process: ExpectedProcess
    generation: str | None = Field(default=None, min_length=1, max_length=128)


class ExpectedLauncher(EvidenceModel):
    kind: Literal["launchd", "crontab", "schtasks"]
    name: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    definition_digest: Digest


class ExpectedUnitWriters(EvidenceModel):
    """Prepared unit inventory, not observed closure or an authorization token."""

    version: Literal[1] = 1
    machine: str = Field(min_length=1, max_length=128)
    home: str
    artifact_digest: Digest
    manifest_digest: Digest
    processes: tuple[ExpectedProcess, ...]
    sessions: tuple[ExpectedSession, ...]
    launchers: tuple[ExpectedLauncher, ...]


class ObservationChallenge(EvidenceModel):
    challenge: UUID
    valid_until: AwareDatetime
