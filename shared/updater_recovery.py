"""Strict durable evidence schemas for updater recovery and continuation."""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from shared.managed_writer_barrier import Digest, EvidenceModel, RolloutIdentity
from shared.managed_writer_closure import LauncherTerminal
from shared.managed_writer_observation import ExpectedUnitWriters, ObservationChallenge
from shared.managed_writer_publication import PublishedUnit, UnitActivationReadback

BootstrapRecoveryStage = Literal[
    "prepared",
    "cron_quiesced",
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


def validate_normal_recovery_transition(
    previous: NormalReleaseRecoveryJournal | None,
    current: NormalReleaseRecoveryJournal,
) -> None:
    """Validate one monotonic journal CAS while retaining activation identity."""
    if previous is None:
        if current.stage != "waiting":
            raise ValueError("normal recovery must begin at waiting")
        return
    if (
        current.request_path,
        current.operation_context,
        current.unit,
        current.previous_selector,
    ) != (
        previous.request_path,
        previous.operation_context,
        previous.unit,
        previous.previous_selector,
    ):
        raise ValueError("normal recovery identity changed")
    allowed: dict[str, frozenset[str]] = {
        "waiting": frozenset({"selected"}),
        "selected": frozenset({"bootstrap_stopped"}),
        "bootstrap_stopped": frozenset({"starting"}),
        "starting": frozenset({"starting", "observed"}),
        "observed": frozenset({"committed"}),
        "committed": frozenset(),
    }
    if current.stage not in allowed[previous.stage]:
        raise ValueError(
            f"normal recovery cannot transition from {previous.stage} to {current.stage}"
        )
    if current.stage == "starting":
        # I8: the single slot may only be replaced with its displaced attempt
        # already adjudicated — an empty slot carries no verdict, an occupied
        # slot demands one plus a fresh nonce (a new attempt is a new attempt).
        if previous.starting_attempt is None:
            if current.replaces is not None:
                raise ValueError("a first start displaces no attempt verdict")
        else:
            if current.replaces is None:
                raise ValueError("replacing a starting attempt requires its adjudication witness")
            if (
                current.starting_attempt is None
                or current.starting_attempt.nonce == previous.starting_attempt.nonce
            ):
                raise ValueError("a replacement start requires a fresh attempt nonce")
    if previous.stage == "observed" and current.readback != previous.readback:
        raise ValueError("committed normal recovery changed observed readback")


class BootstrapRecoveryJournal(EvidenceModel):
    request: str = Field(min_length=1, max_length=4096)
    request_digest: Digest
    inventory_digest: Digest
    candidate_context_digest: Digest
    recovery_context_digest: Digest
    normal_release_planned: bool = False
    stage: BootstrapRecoveryStage
    cron: str = Field(max_length=65536)
    phases: tuple[BootstrapRecoveryPhase, ...] = Field(min_length=1, max_length=64)
    # The hop ledger's launcher facts: one terminal per prepared launcher,
    # written once at ``cron_quiesced`` (the proven quiesce) and carried
    # unchanged afterwards; empty before that write and in journals written
    # before this field existed. The collector consumes these directly.
    launcher_terminals: tuple[LauncherTerminal, ...] = ()
    normal_release: NormalReleaseRecoveryJournal | None = None

    @model_validator(mode="after")
    def coherent_terminal_evidence(self) -> Self:
        if self.phases[-1].stage != self.stage:
            raise ValueError("bootstrap recovery stage requires a matching last phase")
        if self.normal_release is not None and (
            not self.normal_release_planned or self.stage != "candidate_ready"
        ):
            raise ValueError(
                "nested normal recovery requires its planned candidate-ready bootstrap"
            )
        return self
