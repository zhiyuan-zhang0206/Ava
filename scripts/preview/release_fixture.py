"""Package the existing scripted scenario as an explicit proof-only wheel.

This wheel is a declared preview input, never an application dependency or an
artifact purportedly resolved from uv.lock. Its source bytes come from the
captured application archive; it adds no model implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cli.release_prepare.models import FileInput
from shared.posix_command import run_owned_command
from shared.release_identity import ApplicationIdentity
from shared.runtime_release import ReleaseRejectedError, file_sha256

_SOURCE_FILES = (
    "tests/e2e/__init__.py",
    "tests/e2e/fakes/_chat_model.py",
    "tests/e2e/fakes/scenarios/message_flow.py",
)
_PACKAGE_FILES = (
    "tests/__init__.py",
    "tests/e2e/fakes/__init__.py",
    "tests/e2e/fakes/scenarios/__init__.py",
)
_DISTRIBUTION = "ava_preview_fixture-0.0.0.dist-info/"
_PROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ava-preview-fixture"
version = "0.0.0"
description = "Explicit scripted-model input for Ava release proofs only"

[tool.hatch.build.targets.wheel]
packages = ["tests"]
"""


@dataclass(frozen=True)
class FixtureWheel:
    wheel: Path
    digest: str


def _source_bytes(archive: Path, identity: ApplicationIdentity) -> dict[str, bytes]:
    if file_sha256(archive) != identity.source_archive_digest:
        raise ReleaseRejectedError("preview fixture source archive differs from captured identity")
    result: dict[str, bytes] = dict.fromkeys(_PACKAGE_FILES, b"")
    with tarfile.open(archive) as source:
        for name in _SOURCE_FILES:
            members = [member for member in source.getmembers() if member.name == name]
            if len(members) != 1 or not members[0].isfile():
                raise ReleaseRejectedError("preview scenario needs unique regular source files")
            stream = source.extractfile(members[0])
            if stream is None:
                raise ReleaseRejectedError("preview scenario source cannot be read")
            result[name] = stream.read()
    return result


def _verify_wheel(wheel: Path, expected: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        members = {name for name in names if not name.startswith(_DISTRIBUTION)}
        if len(names) != len(set(names)) or members != set(expected):
            raise ReleaseRejectedError("preview fixture wheel changed its declared file roster")
        if any(archive.read(name) != encoded for name, encoded in expected.items()):
            raise ReleaseRejectedError("preview fixture wheel differs from captured scenario")
        metadata = archive.read(_DISTRIBUTION + "METADATA")
        if b"Name: ava-preview-fixture\n" not in metadata or b"Version: 0.0.0\n" not in metadata:
            raise ReleaseRejectedError("preview fixture wheel has an unexpected package identity")


def build_fixture(
    archive: Path,
    identity: ApplicationIdentity,
    work: Path,
    *,
    uv: Path,
    python: Path,
    cache_dir: Path,
    build_constraints: FileInput,
) -> FixtureWheel:
    """Build offline with approved cached tooling; retain failed work for inspection."""
    for path in (archive, work.parent, uv, python, cache_dir):
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ReleaseRejectedError("preview fixture paths must be existing canonical inputs")
    if not work.is_absolute() or ".." in work.parts:
        raise ReleaseRejectedError("preview fixture work must be an absolute exclusive path")
    if file_sha256(build_constraints.path) != build_constraints.digest:
        raise ReleaseRejectedError("preview build tool constraints differ from captured input")
    expected = _source_bytes(archive, identity)
    work.mkdir(mode=0o700)
    source = work / "source"
    source.mkdir(mode=0o700)
    for name, encoded in expected.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
    (source / "pyproject.toml").write_text(_PROJECT)
    wheels = work / "wheels"
    result = run_owned_command(
        [
            str(uv),
            "--no-config",
            "--cache-dir",
            str(cache_dir),
            "--offline",
            "build",
            "--wheel",
            "--no-sources",
            "--python",
            str(python),
            "--out-dir",
            str(wheels),
            "--build-constraints",
            str(build_constraints.path),
            "--require-hashes",
        ],
        cwd=source,
        env={
            "PATH": os.defpath,
            "HOME": str(work),
            "UV_NO_CONFIG": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_DATE_EPOCH": "315532800",
        },
        timeout=300,
        temporary=work,
    )
    result.check_returncode()
    candidates = list(wheels.glob("*.whl"))
    if len(candidates) != 1:
        raise ReleaseRejectedError("preview fixture build must produce exactly one wheel")
    wheel = candidates[0]
    _verify_wheel(wheel, expected)
    if _source_bytes(archive, identity) != expected:
        raise ReleaseRejectedError("preview fixture input changed during its build")
    if file_sha256(build_constraints.path) != build_constraints.digest:
        raise ReleaseRejectedError("preview build tool constraints changed during build")
    digest = file_sha256(wheel)
    receipt = {
        "version": 1,
        "purpose": "proof-only scripted provider; not resolved from uv.lock",
        "source": identity.model_dump(mode="json"),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in expected.items()},
        "wheel": wheel.name,
        "wheel_digest": digest,
        "build_constraints": build_constraints.model_dump(mode="json"),
    }
    (work / "fixture-receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return FixtureWheel(wheel, digest)
