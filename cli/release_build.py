"""Build the application input from one Git commit without changing serving code.

This prepares a wheel and its source receipt, not a runtime or an activation.
The archive, failed builds, and output all stay in a newly allocated directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.migration_layout import required_migration_set_from_names
from shared.proc import run_bounded
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, file_sha256
from shared.verified_file import regular_bytes

_IDENTITY_MEMBER = "shared/release-build.json"


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


def read_application_identity(image: VerifiedRelease, commit: str) -> ApplicationIdentity:
    """Bind an already verified generation to its prepared target commit.

    Full image verification must precede this read. Recheck the exact member
    against that manifest as well; a self-described JSON file is not evidence.
    """
    manifest_path = image.root / "manifest.json"
    # Full runtime inventories are several MiB, unlike individual unit receipts.
    encoded_manifest = regular_bytes(manifest_path, max_bytes=32 * 1024 * 1024)
    if hashlib.sha256(encoded_manifest).hexdigest() != image.manifest_digest:
        raise ReleaseRejectedError("application identity manifest changed")
    manifest = json.loads(encoded_manifest)
    members = [name for name in manifest["files"] if name.endswith("/" + _IDENTITY_MEMBER)]
    if len(members) != 1 or not members[0].startswith("venv/"):
        raise ReleaseRejectedError("verified image requires one installed application identity")
    member = image.root / members[0]
    encoded = regular_bytes(member)
    if hashlib.sha256(encoded).hexdigest() != manifest["files"][members[0]]:
        raise ReleaseRejectedError("application identity differs from verified inventory")
    identity = ApplicationIdentity.model_validate_json(encoded)
    if identity.source_commit != commit or identity.schema_digest != manifest["schema_digest"]:
        raise ReleaseRejectedError("application identity differs from target commit or schema")
    if encoded != _canonical(identity.model_dump(mode="json")):
        raise ReleaseRejectedError("application identity is not canonical")
    return identity


@dataclass(frozen=True)
class ApplicationBuild:
    """The exact wheel produced from the retained committed source archive."""

    wheel: Path
    wheel_digest: str
    source_commit: str
    schema_digest: str
    applied_names: tuple[str, ...]


def _git(repo: Path, *arguments: str) -> str:
    result = run_bounded(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.attributesFile=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ],
        env={
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
        },
        timeout=60,
        capture_output=True,
        text=True,
    )
    result.check_returncode()
    return result.stdout.strip()


def _isolated_objects(repo: Path, destination: Path) -> Path:
    """Expose source objects without its mutable config, attributes or refs."""
    objects = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    ).resolve(strict=True)
    if "\n" in str(objects) or "\r" in str(objects):
        raise ReleaseRejectedError("Git object store path cannot contain a newline")
    isolated = destination / "git"
    _git(destination, "init", "--bare", "--template=", str(isolated))
    (isolated / "objects/info/alternates").write_text(str(objects) + "\n")
    return isolated


def _archive(repo: Path, commit: str, destination: Path) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ReleaseRejectedError("application build requires an exact commit SHA")
    if _git(repo, "cat-file", "-t", commit) != "commit":
        raise ReleaseRejectedError("application build target is not a Git commit")
    destination.mkdir(mode=0o700)  # Never reuse a successful or failed build.
    isolated = _isolated_objects(repo, destination)
    entries = _git(isolated, "ls-tree", "-r", "--name-only", "-z", commit, "--", "migrations")
    names = sorted(
        required_migration_set_from_names(
            entry.rsplit("/", 1)[-1] for entry in entries.split("\0") if entry
        )
    )
    archive = destination / "source.tar"
    _git(isolated, "archive", "--format=tar", f"--output={archive}", commit)
    source = destination / "source"
    source.mkdir(mode=0o700)
    with tarfile.open(archive) as stream:
        stream.extractall(source, filter="data")
    sql_paths = {name for name in entries.split("\0") if name.endswith(".sql")}
    if set(_migration_bytes(source)) != sql_paths:
        raise ReleaseRejectedError("source archive omitted committed migration SQL")
    identity: dict[str, object] = {
        "version": 1,
        "source_commit": commit,
        "source_tree": _git(isolated, "rev-parse", f"{commit}^{{tree}}"),
        "source_archive_digest": file_sha256(archive),
        "schema_digest": file_sha256(source / "db/schema.sql"),
        "applied_names": list(names),
    }
    # The build owns this new member. Refuse a committed lookalike instead of
    # overwriting provenance selected by the target being built.
    with (source / _IDENTITY_MEMBER).open("xb") as stream:
        stream.write(_canonical(identity))
    return identity


def _canonical(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _migration_bytes(source: Path) -> dict[str, bytes]:
    return {
        "migrations/" + path.name: path.read_bytes()
        for path in (source / "migrations").glob("*.sql")
    }


def _verify_migrations(archive: zipfile.ZipFile, expected: dict[str, bytes]) -> None:
    members = [
        name
        for name in archive.namelist()
        if name.startswith("migrations/") and name.endswith(".sql")
    ]
    if len(members) != len(set(members)) or set(members) != set(expected):
        raise ReleaseRejectedError("built wheel migration inventory differs from committed source")
    for name, encoded in expected.items():
        if archive.read(name) != encoded:
            raise ReleaseRejectedError("built wheel migration bytes differ from committed source")


def _verify_wheel(wheel: Path, identity: dict[str, object], migrations: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        if archive.namelist().count(_IDENTITY_MEMBER) != 1:
            raise ReleaseRejectedError("built wheel must contain exactly one source receipt")
        if archive.read(_IDENTITY_MEMBER) != _canonical(identity):
            raise ReleaseRejectedError("built wheel source receipt changed")
        if hashlib.sha256(archive.read("db/schema.sql")).hexdigest() != identity["schema_digest"]:
            raise ReleaseRejectedError("built wheel schema differs from committed source")
        _verify_migrations(archive, migrations)


def build_application(
    repo: Path,
    commit: str,
    destination: Path,
    *,
    uv: Path,
    python: Path,
    cache_dir: Path | None = None,
) -> ApplicationBuild:
    """Build offline from immutable Git input; no checkout, service, or DB writes.

    The caller supplies its existing approved build tools. Missing cached build
    dependencies refuse here; acquiring them belongs to online preparation.
    The receipt records build provenance, not bootability or DB compatibility.
    """
    if not repo.is_absolute() or repo.resolve(strict=True) != repo:
        raise ReleaseRejectedError("build repository must be canonical and absolute")
    if (
        not destination.is_absolute()
        or destination.parent.resolve(strict=True) != destination.parent
    ):
        raise ReleaseRejectedError("build destination parent must be canonical and absolute")
    for tool in (uv, python):
        if not tool.is_absolute() or not tool.is_file():
            raise ReleaseRejectedError("build tools must be explicit existing absolute files")
    if cache_dir is not None and (not cache_dir.is_absolute() or not cache_dir.is_dir()):
        raise ReleaseRejectedError("build cache must be an explicit existing absolute directory")
    identity = _archive(repo, commit, destination)
    source = destination / "source"
    # Snapshot SQL before the backend runs; comparing to its mutable source
    # tree afterwards would let a build hook rewrite both expected and output.
    migrations = _migration_bytes(source)
    wheels = destination / "wheels"
    result = run_bounded(
        [
            str(uv),
            *(["--cache-dir", str(cache_dir)] if cache_dir is not None else []),
            "--offline",
            "build",
            "--wheel",
            "--no-sources",
            "--python",
            str(python),
            "--out-dir",
            str(wheels),
        ],
        cwd=source,
        env={
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "SOURCE_DATE_EPOCH": _git(destination / "git", "show", "-s", "--format=%ct", commit),
        },
        timeout=300,
        capture_output=True,
        text=True,
    )
    result.check_returncode()
    candidates = list(wheels.glob("*.whl"))
    if len(candidates) != 1:
        raise ReleaseRejectedError("application build did not produce exactly one wheel")
    wheel = candidates[0]
    _verify_wheel(wheel, identity, migrations)
    digest = file_sha256(wheel)
    receipt = identity | {"wheel": wheel.name, "wheel_digest": digest}
    with (destination / "build-receipt.json").open("xb") as stream:
        stream.write(_canonical(receipt))
    return ApplicationBuild(
        wheel,
        digest,
        commit,
        str(identity["schema_digest"]),
        ApplicationIdentity.model_validate_json(_canonical(identity)).applied_names,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    result = build_application(
        args.repo,
        args.commit,
        args.output,
        uv=args.uv,
        python=args.python,
        cache_dir=args.cache_dir,
    )
    print(json.dumps({"wheel": str(result.wheel), "wheel_digest": result.wheel_digest}))


if __name__ == "__main__":
    main()
