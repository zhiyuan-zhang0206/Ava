"""The sealed release context a managed-writer rollout is dispatched from (task #4129).

The begin position cannot invent the release it activates: it needs each unit's
sealed candidate and recovery images plus the target's schema baseline and
applied migration names before it may gather prepared facts. That supply is one
canonical private file under the coordinator's ``run/`` directory, named for the
rollout's pinned target (``release-context-<target_sha>.json``): the future
build / first-cutover program writes it before a rollout may enter
managed-writer mode, and the begin position reads it exactly once and snapshots
it -- absent, unreadable, or about another target is a named refusal, never a
fallback.

The file reuses the existing strict models instead of a parallel shape: per-unit
image references are the same ``ImageRef`` the gather dispatches, the recovery
side is the ``PublishedUnit`` binding the operator plan carries, and the
fleet-wide ``schema_digest`` / ``applied_names`` keep the activation-side meaning
(``NormalStartPlan``).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from ops.rpc_prepare_facts import ImageRef
from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.managed_writer_publication import PublishedUnit
from shared.runtime_release import ReleaseRejectedError
from shared.verified_file import regular_bytes


class ReleaseContextUnit(EvidenceModel):
    """One unit's sealed release references for a managed-writer rollout."""

    machine: str = Field(min_length=1, max_length=128)
    candidate: ImageRef
    recovery: PublishedUnit
    recovery_schema_digest: Digest

    @model_validator(mode="after")
    def images_belong_to_the_unit(self) -> Self:
        if self.recovery.machine != self.machine:
            raise ValueError("release context recovery image belongs to another machine")
        if self.recovery.artifact_digest == self.candidate.artifact_digest:
            raise ValueError("release context recovery image must differ from the candidate")
        return self


class ReleaseContext(EvidenceModel):
    """The sealed supply behind one rollout; identical for every participant."""

    version: Literal[1]
    target_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    schema_digest: Digest
    applied_names: tuple[str, ...]
    units: tuple[ReleaseContextUnit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_units(self) -> Self:
        machines = [unit.machine for unit in self.units]
        if machines != sorted(set(machines)):
            raise ValueError("release context units must be unique and sorted by machine")
        if list(self.applied_names) != sorted(set(self.applied_names)):
            raise ValueError("release context applied migration names must be a sorted set")
        for unit in self.units:
            if unit.candidate.schema_digest != self.schema_digest:
                raise ValueError("release context candidate schema differs from the fleet baseline")
        return self


def release_context_path(home: Path, target_sha: str) -> Path:
    """The one sealed file a rollout's begin position reads for `target_sha`."""
    return home / "run" / f"release-context-{target_sha}.json"


def release_context_bytes(context: ReleaseContext) -> bytes:
    """The canonical bytes both the future writer and the reader agree on."""
    return (
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def read_release_context(home: Path, target_sha: str) -> tuple[ReleaseContext, str]:
    """Read the sealed context once; snapshot it with its exact-bytes digest.

    The digest is what the begin position records (log / telemetry), so a later
    reader can tell which supply a rollout dispatched from. Every failure here
    is a named refusal: the begin position must fail closed, it never invents a
    release.
    """
    path = release_context_path(home, target_sha)
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRejectedError(
            f"no sealed release context for target {target_sha} (expected {path})"
        ) from exc
    info = path.stat()
    if (
        canonical != path
        or path.parent != home / "run"
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ReleaseRejectedError("the release context must be a canonical private run file")
    raw = regular_bytes(path)
    try:
        context = ReleaseContext.model_validate_json(raw)
    except ValueError as exc:
        raise ReleaseRejectedError(
            "the sealed release context does not validate as version 1"
        ) from exc
    if context.target_sha != target_sha:
        raise ReleaseRejectedError("the release context belongs to a different rollout target")
    return context, hashlib.sha256(raw).hexdigest()
