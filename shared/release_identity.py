"""The builder-embedded application identity of a verified runtime image.

`cli.release_build` embeds these source facts in the wheel; the image inventory
covers the installed copy. Reads bind an already verified generation to its
target commit. Kept apart from `shared.runtime_release`, which must stay
standard-library only for its environment-free filesystem contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Self

from pydantic import Field, model_validator

from shared.process_evidence import Digest, EvidenceModel
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease

_IDENTITY_MEMBER = "shared/release-build.json"
_APPLICATION_MEMBER_PATTERN = re.compile(
    r"venv/(?:lib/python[0-9]+\.[0-9]+|Lib)/site-packages/shared/release-build\.json"
)


class ApplicationIdentity(EvidenceModel):
    """Source facts embedded by the builder and covered by the image inventory."""

    version: Literal[1]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_archive_digest: Digest
    schema_digest: Digest
    applied_names: tuple[str, ...]

    @model_validator(mode="after")
    def ordered_names(self) -> Self:
        if not self.applied_names or list(self.applied_names) != sorted(set(self.applied_names)):
            raise ValueError("build migration names must be a nonempty sorted set")
        return self


def application_identity_members(files: dict[str, str], platform: str) -> list[str]:
    """Admit one install and its exact Linux lib64 materialization, if present."""
    members = sorted(name for name in files if name.endswith("/" + _IDENTITY_MEMBER))
    primary = [name for name in members if _APPLICATION_MEMBER_PATTERN.fullmatch(name)]
    if len(primary) != 1:
        raise ReleaseRejectedError("verified image requires one installed application identity")
    allowed = {primary[0]}
    if platform.startswith("Linux-") and primary[0].startswith("venv/lib/"):
        # Runtime preparation replaces stdlib venv's lib64 -> lib symlink with
        # a private directory copy. Both physical copies must agree below.
        allowed.add(primary[0].replace("venv/lib/", "venv/lib64/", 1))
    if not set(members) <= allowed:
        raise ReleaseRejectedError("verified image has an unexpected application identity copy")
    return members


def read_application_identity(image: VerifiedRelease, commit: str) -> ApplicationIdentity:
    """Bind an already verified generation to its prepared target commit.

    Full image verification must precede this read. Recheck the exact member
    against that manifest as well; a self-described JSON file is not evidence.
    """
    from shared.verified_file import regular_bytes

    manifest_path = image.root / "manifest.json"
    # Full runtime inventories are several MiB, unlike individual unit receipts.
    encoded_manifest = regular_bytes(manifest_path, max_bytes=32 * 1024 * 1024)
    if hashlib.sha256(encoded_manifest).hexdigest() != image.manifest_digest:
        raise ReleaseRejectedError("application identity manifest changed")
    manifest = json.loads(encoded_manifest)
    members = application_identity_members(manifest["files"], manifest["platform"])
    copies = [regular_bytes(image.root / name) for name in members]
    for name, encoded in zip(members, copies, strict=True):
        if hashlib.sha256(encoded).hexdigest() != manifest["files"][name]:
            raise ReleaseRejectedError("application identity differs from verified inventory")
    encoded = copies[0]
    if any(copy != encoded for copy in copies[1:]):
        raise ReleaseRejectedError("installed application identity copies disagree")
    identity = ApplicationIdentity.model_validate_json(encoded)
    if identity.source_commit != commit or identity.schema_digest != manifest["schema_digest"]:
        raise ReleaseRejectedError("application identity differs from target commit or schema")
    if (
        encoded
        != (
            json.dumps(identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
    ):
        raise ReleaseRejectedError("application identity is not canonical")
    return identity
