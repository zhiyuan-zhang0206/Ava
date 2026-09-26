"""Captured requests and derivation evidence for online input acquisition."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from cli.release_prepare.models import (
    Commit,
    FileInput,
    LocalInputs,
    Record,
    TreeInput,
    _absolute,
    ordered_plugins,
)
from shared.runtime_release import ApplicationIdentity


class FrontendTools(Record):
    node: FileInput
    npm: TreeInput
    gateway_port: int = Field(ge=1, le=65535)


class Acquisition(Record):
    version: Literal[1] = 1
    repo: Path
    commit: Commit
    work: Path
    uv: FileInput
    build_constraints: FileInput
    source_distributions: TreeInput | None = None
    frontend: FrontendTools | None = None
    collector: bool = False
    plugins: TreeInput | None = None
    required_plugins: tuple[str, ...] = ()

    _paths = field_validator("repo", "work")(_absolute)
    _plugins = field_validator("required_plugins")(ordered_plugins)


class WheelDerivation(Record):
    kind: Literal["downloaded-wheel", "built-wheel"]
    source: FileInput
    wheel: FileInput
    package: str
    package_version: str


class CommandEvidence(Record):
    argv: tuple[str, ...]
    cwd: Path
    output: FileInput
    started_at: AwareDatetime
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    returncode: int | None
    timed_out: bool = False


class AcquisitionReceipt(Record):
    version: Literal[1] = 1
    request: Acquisition
    source: ApplicationIdentity
    archive: FileInput
    source_lock: FileInput
    source_inputs: dict[str, FileInput]
    exported_requirements: FileInput
    source_distributions: TreeInput
    build_tools: TreeInput
    derivations: tuple[WheelDerivation, ...]
    commands: tuple[CommandEvidence, ...]
    platform: str
    python_version: str
    pip_version: str
    uv_version: str
    inputs: LocalInputs

    @model_validator(mode="after")
    def bound_source_and_inputs(self) -> Self:
        if (
            self.source.source_commit != self.request.commit
            or self.archive.digest != self.source.source_archive_digest
            or self.archive.path != self.request.work / "captured/source.tar"
            or "uv.lock" not in self.source_inputs
            or self.source_inputs["uv.lock"] != self.source_lock
            or self.source_lock.digest != self.inputs.source_lock_digest
            or self.inputs.build_constraints.digest != self.request.build_constraints.digest
            or self.inputs.uv != self.request.uv
        ):
            raise ValueError("acquisition receipt source and inputs disagree")
        for edge in self.derivations:
            if (
                edge.source.path.parent != self.source_distributions.root
                or edge.wheel.path.parent != self.inputs.wheelhouse.root
            ):
                raise ValueError("wheel derivation escaped captured acquisition inputs")
            if edge.kind == "downloaded-wheel" and edge.source.digest != edge.wheel.digest:
                raise ValueError("downloaded wheel differs from its source distribution")
        return self
