"""The frozen v1 image-exec handoff: the only contract that crosses release versions.

A release is run by the code of the image it installs. The previous image only
reads a request's envelope, verifies the named image in its home's store and
runs a fixed entry point of that image; the candidate does everything else.
Changing anything here (the envelope reader, the exec argv, the entry names or
the `release_image_exec` wire models) takes a two-step release: ship a reader
of the new form first, use it one release later. Settings-free, because the
previous image's CLI handoff and the candidate's entry read it before any
configuration loads.

**Envelope.** Every request document carries these top-level fields; the
reader ignores every other field and kind, so a newer candidate can add
request kinds and fields without a second release.

| Field | Meaning |
|---|---|
| `version` | the JSON integer `1` (not `true`, `1.0` or `"1"`): the handoff envelope version |
| `kind` | the request kind; opaque to the reader |
| `id` | the operation id, a canonical lowercase UUID string |
| `home` | the absolute canonical home the request belongs to |
| `machine` | the machine name of that home's unit |
| `executor` | the image that runs the request on this host (`ReleaseImageRef` fields; later fields ignored) |

**Exec.** The executor's verified interpreter runs
`-I -B -X utf8 -m cli.release_handoff ENTRY SOURCE` in the image's working
directory with the caller's environment plus `AVA_HOME=<envelope home>`;
`SOURCE` is the request path or `-` for the exact bytes on stdin.

**Wire.** `release_image_exec` carries `{entry, image, request (base64)}` and
returns `{entry, result}`; every wire model is closed (`extra="forbid"`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shared.release_identity import read_application_identity
from shared.runtime_abi import AbiTag
from shared.runtime_release import VerifiedRelease, verify_release

ReleaseImageEntry = Literal["receipt", "preflight", "submit"]
ENTRY_MODULE = "cli.release_handoff"
# The largest request document one handoff carries (base64 on the wire).
RELEASE_REQUEST_MAX_BYTES = 1024 * 1024
_SHA256 = r"^[0-9a-f]{64}$"
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class HandoffRefusedError(ValueError):
    """A handoff that must not run: no v1 envelope, another home or unit, an
    image that does not verify, or an entry that failed."""


class ReleaseImageRef(BaseModel):
    """One retained image: the digests that pin its bytes and its identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_digest: str = Field(pattern=_SHA256)
    manifest_digest: str = Field(pattern=_SHA256)
    schema_digest: str = Field(pattern=_SHA256)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")

    def verify(self, home: Path, *, host_abi: AbiTag) -> VerifiedRelease:
        """Verify the image in `home`'s store against `host_abi` (observed now):
        every file hash, the schema identity and the packaged source commit."""
        image = verify_release(
            home / "releases",
            self.artifact_digest,
            manifest_digest=self.manifest_digest,
            host_abi=host_abi,
            schema_digest=self.schema_digest,
        )
        read_application_identity(image, self.source_commit)
        return image


class ReleaseImageExecPayload(BaseModel):
    """`release_image_exec` payload: run `entry` of `image` on `request`.

    `request` is the base64 of the exact request document; its envelope must
    name this unit (home and machine) and `image` as executor. The op changes
    nothing itself: it verifies the image in the unit's store and runs that
    entry with bounded time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: ReleaseImageEntry
    image: ReleaseImageRef
    request: str = Field(min_length=1, max_length=(RELEASE_REQUEST_MAX_BYTES + 2) // 3 * 4)


class ReleaseImageExecResult(BaseModel):
    """`release_image_exec` result: the entry's own JSON object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: ReleaseImageEntry
    result: dict[str, Any]


class EnvelopeImage(ReleaseImageRef):
    """The executor reference inside a request document (later fields ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class Envelope(BaseModel):
    """The v1 envelope fields of a request document, each in its exact JSON spelling.

    `strict=True` alone still admits JSON `true` and `1.0` for `Literal[1]` and
    any UUID spelling (uppercase, unhyphenated, braced, `urn:uuid:`) for `id`;
    a frozen reader can never be tightened later, so both are pinned here.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    version: Literal[1]
    kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    id: UUID
    home: str
    machine: Annotated[str, Field(min_length=1, max_length=128)]
    executor: EnvelopeImage

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("the envelope version must be the JSON integer 1")
        return value

    @field_validator("id", mode="before")
    @classmethod
    def canonical_id(cls, value: object) -> object:
        if not isinstance(value, str) or _CANONICAL_UUID.fullmatch(value) is None:
            raise ValueError("the envelope id must be a canonical lowercase UUID string")
        return value

    @field_validator("home")
    @classmethod
    def canonical_home(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or str(path) != value or ".." in path.parts:
            raise ValueError("the envelope home must be normalized and absolute")
        if any(ord(char) < 32 for char in value):
            raise ValueError("the envelope home contains control characters")
        return value

    def names(self, image: ReleaseImageRef) -> bool:
        """Whether `image` is exactly this request's executor."""
        return ReleaseImageRef.model_validate(self.executor.model_dump()) == image


def read_envelope(encoded: bytes) -> Envelope:
    if len(encoded) > RELEASE_REQUEST_MAX_BYTES:
        raise HandoffRefusedError("the request document exceeds the handoff size limit")
    try:
        return Envelope.model_validate_json(encoded)
    except ValidationError as exc:
        raise HandoffRefusedError(f"the request carries no v1 handoff envelope: {exc}") from exc


def entry_argv(image: VerifiedRelease, entry: ReleaseImageEntry, source: str) -> tuple[str, ...]:
    return image.module_argv(ENTRY_MODULE, entry, source)


def entry_environment(base: Mapping[str, str], home: str) -> dict[str, str]:
    """The caller's environment with the explicit home an installed image requires."""
    return {**base, "AVA_HOME": home}
