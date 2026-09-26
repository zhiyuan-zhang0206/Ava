"""Explicit local build inputs and source-to-image preparation evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.release_identity import ApplicationIdentity

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise ValueError("preparation paths must be normalized absolute paths")
    return path


class TreeInput(Record):
    root: Path
    digest: Digest

    _root = field_validator("root")(_absolute)


class FileInput(Record):
    path: Path
    digest: Digest

    _path = field_validator("path")(_absolute)


def ordered_plugins(names: tuple[str, ...]) -> tuple[str, ...]:
    if list(names) != sorted(set(names)) or any(
        not name or Path(name).name != name or name.startswith(".") for name in names
    ):
        raise ValueError("required plugins must be sorted unique package names")
    return names


class LocalInputs(Record):
    """Trusted supplied artifacts; hashes alone do not attest lock resolution."""

    version: Literal[1] = 1
    python: TreeInput
    wheelhouse: TreeInput
    requirements: FileInput
    source_lock_digest: Digest
    build_constraints: FileInput
    uv: FileInput
    cache_dir: Path
    frontend: TreeInput | None = None
    collector: TreeInput | None = None
    plugins: TreeInput | None = None
    required_plugins: tuple[str, ...] = ()

    _cache = field_validator("cache_dir")(_absolute)
    _plugins = field_validator("required_plugins")(ordered_plugins)

    @model_validator(mode="after")
    def declared_plugins(self) -> Self:
        if self.required_plugins and self.plugins is None:
            raise ValueError("required plugins need a supplied plugin tree")
        return self


class Preparation(Record):
    version: Literal[1] = 1
    repo: Path
    commit: Commit
    work: Path
    store: Path
    inputs: LocalInputs

    _paths = field_validator("repo", "work", "store")(_absolute)


class BuildEvidence(Record):
    wheel: str
    wheel_digest: Digest
    receipt_digest: Digest
    source_lock_digest: Digest
    combined_wheelhouse_digest: Digest


class ImageEvidence(Record):
    artifact_digest: Digest
    manifest_digest: Digest
    schema_digest: Digest
    platform: str = Field(min_length=1)
    root: Path
    interpreter: Path
    cwd: Path

    _paths = field_validator("root", "interpreter", "cwd")(_absolute)


class PreparationReceipt(Record):
    version: Literal[1] = 1
    request: Preparation
    request_digest: Digest
    source: ApplicationIdentity
    build: BuildEvidence
    image: ImageEvidence

    @model_validator(mode="after")
    def bound_image(self) -> Self:
        if self.request_digest != hashlib.sha256(encode(self.request)).hexdigest():
            raise ValueError("preparation receipt differs from its captured request")
        if (
            self.source.source_commit != self.request.commit
            or self.source.schema_digest != self.image.schema_digest
            or self.build.source_lock_digest != self.request.inputs.source_lock_digest
            or self.image.root != self.request.store / self.image.artifact_digest
            or not self.image.interpreter.is_relative_to(self.image.root)
            or not self.image.cwd.is_relative_to(self.image.root)
        ):
            raise ValueError("preparation receipt source, inputs and image disagree")
        return self


def encode(record: BaseModel) -> bytes:
    return (
        json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
