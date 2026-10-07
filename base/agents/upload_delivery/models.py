"""Strict delivered-upload identity and versioned receiver proof."""

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base.agents.uploads import MAX_UPLOAD_BYTES
from base.cluster.authority.unit import UnitIdentity
from base.cluster.machine import machine_name
from base.paths import ava_home

NAMESPACE = ".delivered-v1"
BATCH_ID_PATTERN = r"^[0-9a-f]{32}$"
# Reserve the native create_private_bytes dot/UUID/temp-name overhead (38 bytes).
MAX_OBJECT_NAME_BYTES = 217
OBJECT_NAME_PATTERN = r"^[0-9a-f]{32}-[0-9]+(?:\.[^/\\\x00-\x1f\x7f?#%]+)?$"


class UploadDeliveryConflictError(ValueError):
    """The immutable intent, object or frozen native placement no longer matches."""


class UploadQuotaExceededError(ValueError):
    """Native physical uploads and receiving reservations exhaust the quota."""


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Object(Record):
    ordinal: int = Field(ge=0, lt=1000)
    filename: str = Field(min_length=1, max_length=4096)
    name: str = Field(pattern=OBJECT_NAME_PATTERN)
    size: int = Field(ge=0, le=MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=255, pattern=r"^[\x20-\x7e]+$")

    @model_validator(mode="after")
    def bounded_name(self) -> Self:
        if len(self.name.encode()) > MAX_OBJECT_NAME_BYTES or "\x00" in self.filename:
            raise ValueError("upload name exceeds the immutable filesystem contract")
        return self


class Manifest(Record):
    batch_id: str = Field(pattern=BATCH_ID_PATTERN)
    agent_id: int = Field(gt=0, lt=2**63)
    objects: list[Object] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def ordered_names(self) -> Self:
        for ordinal, item in enumerate(self.objects):
            if (
                item.ordinal != ordinal
                or item.name.split(".", 1)[0] != f"{self.batch_id}-{ordinal}"
            ):
                raise ValueError("manifest objects require ordered batch-owned names")
        if len({item.name for item in self.objects}) != len(self.objects):
            raise ValueError("manifest object names must be unique")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class VersionedRecord(Record):
    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def explicit_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("upload protocol version must be the explicit integer 1")
        return value


class ReceiveRequest(VersionedRecord):
    target: UnitIdentity
    source: UnitIdentity
    manifest: Manifest


class CopyProof(VersionedRecord):
    target: UnitIdentity
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    directory: str = Field(min_length=1)

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value) or not (
            PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
        ):
            raise ValueError("copy proof requires an absolute native directory")
        return value


class Acceptance(Record):
    """Historical source acceptance; not copy readiness or agent execution."""

    batch_id: str
    agent_id: int
    files: list[Object]
    status_url: str


class DeliveryStatus(Record):
    acceptance: Acceptance | None
    state: Literal["receiving", "pending", "accepted", "hold"]
    reason: str | None
    inbound_id: int | None
    source: UnitIdentity
    target: UnitIdentity
    attempts: int = Field(ge=0)
    next_attempt_at: datetime | None


def current_unit() -> UnitIdentity:
    return UnitIdentity(machine=machine_name(), home=str(ava_home().resolve()))


def make_manifest(agent_id: int, names: list[str], batch: list[tuple[str, bytes, str]]) -> Manifest:
    batch_id = uuid.uuid4().hex
    objects: list[Object] = []
    for ordinal, (name, (_, contents, content_type)) in enumerate(zip(names, batch, strict=True)):
        suffix = Path(name.replace("\\", "/")).suffix
        objects.append(
            Object(
                ordinal=ordinal,
                filename=name,
                name=f"{batch_id}-{ordinal}{suffix}",
                size=len(contents),
                sha256=hashlib.sha256(contents).hexdigest(),
                content_type=content_type,
            )
        )
    return Manifest(batch_id=batch_id, agent_id=agent_id, objects=objects)


def request_hash(names: list[str], batch: list[tuple[str, bytes, str]]) -> str:
    value = [
        [name, len(contents), hashlib.sha256(contents).hexdigest(), content_type]
        for name, (_, contents, content_type) in zip(names, batch, strict=True)
    ]
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
