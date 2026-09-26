"""Strict schemas for the retained updater recovery evidence.

The retired updater wrote these journals; `shared.updater_handoff` only reads
them now, to refuse generic recovery while one is unfinished or malformed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_closure import LauncherTerminal
from shared.managed_writer_observation import ExpectedUnitWriters, ObservationChallenge
from shared.managed_writer_publication import PublishedUnit, UnitActivationReadback
from shared.process_evidence import Digest, EvidenceModel

BootstrapRecoveryStage = Literal[
    "prepared",
    "cron_quiesced",
    "launchers_quiesced",
    "old_stopped",
    "candidate_starting",
    "candidate_started",
    "candidate_ready",
    "recovering",
    "recovered",
]


class BootstrapRecoveryPhase(EvidenceModel):
    stage: BootstrapRecoveryStage
    observed_at: dt.datetime
    monotonic_s: float
    pid: int
    elapsed_s: float | None


class PreparedObservationRecovery(EvidenceModel):
    expected: ExpectedUnitWriters
    operation: RolloutIdentity
    challenge: ObservationChallenge
    schema_digest: Digest


SpawnVerdict = Literal["spawned_alive", "spawned_dead", "not_spawned"]
"""The three adjudicable outcomes of one spawn attempt (ambiguous is never a write)."""


class SpawnAttempt(EvidenceModel):
    """One exact gated spawn attempt's journal facts (design #4117 §5.6).

    ``spawn_lock_path`` / ``receipt_path`` are ``$AVA_HOME``-relative: the
    journal stays portability-stable evidence, while the files themselves are
    the receipt (per-attempt, ``<session>.<nonce>.receipt.json``) and the gate
    (per-session, ``<session>.gate`` — the nonce names the attempt, never the
    gate). Their shape is pinned here so a slot can never be written with
    paths that the adjudicator would not recognize: same directory, gate named
    for the session, receipt named for session + nonce.
    """

    version: Literal[1] = 1
    nonce: UUID
    session: str = Field(min_length=1, max_length=128)
    cmd_digest: Digest
    cwd: str = Field(min_length=1, max_length=4096)
    spawn_lock_path: str = Field(min_length=1, max_length=4096)
    receipt_path: str = Field(min_length=1, max_length=4096)
    recorded_at: AwareDatetime

    @field_validator("spawn_lock_path", "receipt_path")
    @classmethod
    def relative_normalized_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or str(path) != value or not value:
            raise ValueError("spawn attempt paths must be normalized and $AVA_HOME-relative")
        return value

    @model_validator(mode="after")
    def coherent_attempt_paths(self) -> Self:
        receipt = PurePosixPath(self.receipt_path)
        gate = PurePosixPath(self.spawn_lock_path)
        if receipt.parent != gate.parent:
            raise ValueError("spawn attempt receipt and gate must share their directory")
        if gate.name != f"{self.session}.gate":
            raise ValueError("spawn attempt gate must be named for its session")
        if receipt.name != f"{self.session}.{self.nonce}.receipt.json":
            raise ValueError("spawn attempt receipt must be named for its session and nonce")
        return self


class NormalReleaseRecoveryJournal(EvidenceModel):
    request_path: str = Field(min_length=1, max_length=4096)
    operation_context: PreparedObservationRecovery
    unit: PublishedUnit
    previous_selector: str | None = Field(max_length=65536)
    stage: Literal["waiting", "selected", "bootstrap_stopped", "starting", "observed", "committed"]
    starting_session: str | None = Field(default=None, min_length=1, max_length=128)
    starting_attempt: SpawnAttempt | None = None
    # The adjudicated verdict of the attempt this write displaces — the I8
    # witness that no unadjudicated attempt is evicted (mandatory when the
    # single slot was occupied, forbidden when it was empty).
    replaces: SpawnVerdict | None = None
    readback: UnitActivationReadback | None = None

    @model_validator(mode="after")
    def coherent_phase(self) -> Self:
        if self.stage == "starting":
            if (
                self.starting_session is None
                or self.starting_attempt is None
                or self.readback is not None
            ):
                raise ValueError(
                    "starting normal recovery requires only its exact session and attempt"
                )
            if self.starting_attempt.session != self.starting_session:
                raise ValueError("starting attempt must name its exact session")
        elif self.stage in {"observed", "committed"}:
            if (
                self.starting_session is not None
                or self.starting_attempt is not None
                or self.readback is None
            ):
                raise ValueError("observed normal recovery requires its complete readback")
        elif (
            self.starting_session is not None
            or self.starting_attempt is not None
            or self.readback is not None
        ):
            raise ValueError("pre-observation normal recovery cannot carry later evidence")
        if self.replaces is not None and (
            self.stage != "starting" or self.starting_attempt is None
        ):
            raise ValueError("replaces witnesses only a replaced starting attempt")
        return self


class LaunchdRecovery(EvidenceModel):
    """Exact private original for one admitted restricted bootstrap launcher."""

    label: str = Field(pattern=r"^com\.ava\.[A-Za-z0-9_.-]{1,247}$")
    definition: str = Field(min_length=1, max_length=65536)
    loaded: Literal[False]
    custody: str = Field(min_length=1, max_length=4096)
    mode: Literal[384, 420]  # Owner-write-only 0600 or 0644 definitions.


class BootstrapRecoveryJournal(EvidenceModel):
    request: str = Field(min_length=1, max_length=4096)
    request_digest: Digest
    inventory_digest: Digest
    candidate_context_digest: Digest
    recovery_context_digest: Digest
    normal_release_planned: bool = False
    stage: BootstrapRecoveryStage
    cron: str = Field(max_length=65536)
    launchd: tuple[LaunchdRecovery, ...] = Field(default=(), max_length=64)
    phases: tuple[BootstrapRecoveryPhase, ...] = Field(min_length=1, max_length=64)
    # The hop ledger's launcher facts: one terminal per prepared launcher,
    # written once at the proven native quiesce and carried
    # unchanged afterwards; empty before that write and in journals written
    # before this field existed. The collector consumes these directly.
    launcher_terminals: tuple[LauncherTerminal, ...] = ()
    normal_release: NormalReleaseRecoveryJournal | None = None

    @model_validator(mode="after")
    def coherent_terminal_evidence(self) -> Self:
        labels = [item.label for item in self.launchd]
        if labels != sorted(set(labels)) or (self.cron and self.launchd):
            raise ValueError("bootstrap native originals must be unique and one scheduler family")
        if sum(len(item.definition.encode()) for item in self.launchd) > 65536:
            raise ValueError("bootstrap launchd originals exceed their byte budget")
        if self.stage == "launchers_quiesced" and not self.launchd:
            raise ValueError("launchd quiesce requires retained native originals")
        if self.phases[-1].stage != self.stage:
            raise ValueError("bootstrap recovery stage requires a matching last phase")
        if self.normal_release is not None and (
            not self.normal_release_planned or self.stage != "candidate_ready"
        ):
            raise ValueError(
                "nested normal recovery requires its planned candidate-ready bootstrap"
            )
        return self
