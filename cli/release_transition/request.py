"""Captured release inputs, independent of Settings and moving source refs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release
from shared.start_inputs import files_digest, require_configuration

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReleaseRef(Record):
    artifact_digest: Digest
    manifest_digest: Digest
    schema_digest: Digest
    source_commit: Commit

    def verify(self, home: Path, platform_tag: str) -> VerifiedRelease:
        """Verify all retained bytes and the separately captured source receipt."""
        from shared.runtime_release import read_application_identity

        image = verify_release(
            home / "releases",
            self.artifact_digest,
            manifest_digest=self.manifest_digest,
            platform_tag=platform_tag,
            schema_digest=self.schema_digest,
        )
        read_application_identity(image, self.source_commit)
        return image

    @property
    def selector(self) -> tuple[str, str]:
        return self.artifact_digest, self.manifest_digest


class HomeRequest(Record):
    version: Literal[1] = 1
    id: UUID
    home: str
    registry: str
    created_at: AwareDatetime
    platform_tag: str = Field(min_length=1, max_length=128)
    machine: str = Field(min_length=1, max_length=128)
    configuration_digest: Digest

    @field_validator("home", "registry")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or str(path) != value or ".." in path.parts:
            raise ValueError("release operation paths must be normalized and absolute")
        if any(ord(char) < 32 for char in value):
            raise ValueError("release operation paths contain control characters")
        return value

    @property
    def path(self) -> Path:
        return Path(self.home) / "updates" / str(self.id) / "operation.json"

    def require_configuration(self) -> None:
        """The captured request cannot admit a later configuration generation."""
        require_configuration(Path(self.home), self.configuration_digest)


class Request(HomeRequest):
    kind: Literal["release"] = "release"
    previous: ReleaseRef
    candidate: ReleaseRef
    executor: ReleaseRef

    @model_validator(mode="after")
    def fixed_executor(self) -> Self:
        if self.executor != self.candidate:
            raise ValueError("the prepared candidate must supply the retained executor")
        if self.previous.selector == self.candidate.selector:
            raise ValueError("a release operation requires distinct previous and candidate images")
        return self


class PitrRequest(HomeRequest):
    kind: Literal["pitr"] = "pitr"
    image: ReleaseRef
    activation_id: UUID
    action: Literal["activate", "rollback"]
    generation: int = Field(ge=1)
    expected_record: Digest | None
    origin: str = Field(min_length=1, max_length=256)
    configuration_files: dict[str, Digest | None]

    @model_validator(mode="after")
    def rollback_requires_record(self) -> Self:
        if self.action == "rollback" and self.expected_record is None:
            raise ValueError("PITR rollback requires an existing activation record")
        if (
            ".env" not in self.configuration_files
            or files_digest(self.configuration_files) != self.configuration_digest
        ):
            raise ValueError("PITR configuration inventory differs from the captured digest")
        return self

    @property
    def executor(self) -> ReleaseRef:
        return self.image


AnyRequest = Annotated[Request | PitrRequest, Field(discriminator="kind")]
_REQUEST: TypeAdapter[Request | PitrRequest] = TypeAdapter(AnyRequest)


def read_request(encoded: bytes) -> Request | PitrRequest:
    return _REQUEST.validate_json(encoded)


def sql_inventory(image: VerifiedRelease) -> dict[str, str]:
    """Compare actual paired SQL, not only the squashed baseline's digest.

    The complete image has already been verified. A different up/down script
    under an unchanged db/schema.sql is still a schema transition.
    """
    manifest = json.loads((image.root / "manifest.json").read_bytes())
    inventories: dict[str, dict[str, str]] = {}
    for name, digest in manifest["files"].items():
        marker = "/site-packages/migrations/"
        if marker in name and name.endswith(".sql"):
            prefix, relative = name.split(marker, 1)
            inventories.setdefault(prefix, {})[relative] = digest
    copies = list(inventories.values())
    if not copies or any(copy != copies[0] for copy in copies[1:]):
        raise ReleaseRejectedError("release has missing or inconsistent migration inventories")
    return copies[0]


def verify_pair(request: Request) -> tuple[VerifiedRelease, VerifiedRelease]:
    """Admit the first connected same-schema transition before any outage.

    Schema-changing transitions need the fleet writer fence and migration
    recovery proof. Until that path is connected they refuse before quiescing.
    """
    home = Path(request.home)
    if home.resolve(strict=True) != home or home.is_symlink():
        raise ReleaseRejectedError("release operation home must be canonical and existing")
    previous = request.previous.verify(home, request.platform_tag)
    candidate = request.candidate.verify(home, request.platform_tag)
    if request.previous.schema_digest != request.candidate.schema_digest:
        raise ReleaseRejectedError("schema-changing release requires the fleet migration barrier")
    if sql_inventory(previous) != sql_inventory(candidate):
        raise ReleaseRejectedError("changed migration SQL requires the fleet migration barrier")
    return previous, candidate
