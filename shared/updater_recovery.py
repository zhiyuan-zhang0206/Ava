"""Strict durable evidence schemas for updater recovery and continuation."""

from __future__ import annotations

import datetime as dt
from typing import Literal, Self

from pydantic import Field, model_validator

from shared.managed_writer_barrier import Digest, EvidenceModel, RolloutIdentity
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


class NormalReleaseRecoveryJournal(EvidenceModel):
    request_path: str = Field(min_length=1, max_length=4096)
    operation_context: PreparedObservationRecovery
    unit: PublishedUnit
    previous_selector: str | None = Field(max_length=65536)
    stage: Literal["waiting", "selected", "bootstrap_stopped", "starting", "observed", "committed"]
    starting_session: str | None = Field(default=None, min_length=1, max_length=128)
    readback: UnitActivationReadback | None = None

    @model_validator(mode="after")
    def coherent_phase(self) -> Self:
        if self.stage == "starting":
            if self.starting_session is None or self.readback is not None:
                raise ValueError("starting normal recovery requires only its exact session")
        elif self.stage in {"observed", "committed"}:
            if self.starting_session is not None or self.readback is None:
                raise ValueError("observed normal recovery requires its complete readback")
        elif self.starting_session is not None or self.readback is not None:
            raise ValueError("pre-observation normal recovery cannot carry later evidence")
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
