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

from shared.migration_layout import required_migration_set_from_names
from shared.posix_command import run_owned_command
from shared.release_identity import ApplicationIdentity
from shared.runtime_release import ReleaseRejectedError, file_sha256
from shared.verified_file import regular_bytes

_IDENTITY_MEMBER = "shared/release-build.json"


@dataclass(frozen=True)
class ApplicationBuild:
    """The exact wheel produced from the retained committed source archive."""

    wheel: Path
    wheel_digest: str
    source_commit: str
    schema_digest: str
    applied_names: tuple[str, ...]


def _git(repo: Path, *arguments: str) -> str:
    result = run_owned_command(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.attributesFile=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ],
        cwd=repo,
        env={
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
        },
        timeout=60,
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


def _source_epoch(repo: Path, commit: str) -> str:
    # Pretty-printing a commit can parse its parent even with `show -s`.
    # Read the object itself so a depth-one CI checkout needs no parent/history.
    header = _git(repo, "cat-file", "commit", commit).split("\n\n", 1)[0]
    lines = [line for line in header.splitlines() if line.startswith("committer ")]
    match = re.fullmatch(r"committer .+ (\d+) [+-]\d{4}", lines[0]) if len(lines) == 1 else None
    if match is None:
        raise ReleaseRejectedError("source commit has no supported committer epoch")
    return match[1]


@dataclass(frozen=True)
class CapturedSource:
    """Retained committed source shared by acquisition and application building."""

    identity: ApplicationIdentity
    archive: Path
    source: Path


def capture_source(repo: Path, commit: str, destination: Path) -> CapturedSource:
    """Capture one exact commit into a new directory without loading its code."""
    if not repo.is_absolute() or repo.resolve(strict=True) != repo:
        raise ReleaseRejectedError("source repository must be canonical and absolute")
    if (
        not destination.is_absolute()
        or destination.parent.resolve(strict=True) != destination.parent
    ):
        raise ReleaseRejectedError("source destination parent must be canonical and absolute")
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
    return CapturedSource(
        ApplicationIdentity.model_validate_json(_canonical(identity)), archive, source
    )


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


def _build_inputs(uv: Path, python: Path, build_constraints: Path, cache_dir: Path | None) -> bytes:
    for tool in (uv, python):
        if not tool.is_absolute() or not tool.is_file():
            raise ReleaseRejectedError("build tools must be explicit existing absolute files")
    if cache_dir is not None and (not cache_dir.is_absolute() or not cache_dir.is_dir()):
        raise ReleaseRejectedError("build cache must be an explicit existing absolute directory")
    constraints = regular_bytes(build_constraints)
    if (
        not build_constraints.is_absolute()
        or build_constraints.resolve(strict=True) != build_constraints
    ):
        raise ReleaseRejectedError("build constraints must be an explicit canonical file")
    return constraints


def build_application(
    repo: Path,
    commit: str,
    destination: Path,
    *,
    uv: Path,
    python: Path,
    build_constraints: Path,
    cache_dir: Path | None = None,
) -> ApplicationBuild:
    """Build offline from immutable Git input; no checkout, service, or DB writes.

    The caller supplies its existing approved build tools. Missing cached build
    dependencies refuse here; acquiring them belongs to online preparation.
    The receipt records build provenance, not bootability or DB compatibility.
    """
    constraints = _build_inputs(uv, python, build_constraints, cache_dir)
    captured = capture_source(repo, commit, destination)
    identity: dict[str, object] = captured.identity.model_dump(mode="json")
    source = captured.source
    private_constraints = destination / "build-constraints.txt"
    with private_constraints.open("xb") as stream:
        stream.write(constraints)
    # Snapshot SQL before the backend runs; comparing to its mutable source
    # tree afterwards would let a build hook rewrite both expected and output.
    migrations = _migration_bytes(source)
    wheels = destination / "wheels"
    result = run_owned_command(
        [
            str(uv),
            *(["--cache-dir", str(cache_dir)] if cache_dir is not None else []),
            "--no-config",
            "--offline",
            "build",
            "--wheel",
            "--no-sources",
            "--build-constraints",
            str(private_constraints),
            "--require-hashes",
            "--python",
            str(python),
            "--out-dir",
            str(wheels),
        ],
        cwd=source,
        env={
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "SOURCE_DATE_EPOCH": _source_epoch(destination / "git", commit),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        timeout=300,
        temporary=destination,
    )
    result.check_returncode()
    candidates = list(wheels.glob("*.whl"))
    if len(candidates) != 1:
        raise ReleaseRejectedError("application build did not produce exactly one wheel")
    wheel = candidates[0]
    _verify_wheel(wheel, identity, migrations)
    digest = file_sha256(wheel)
    if (
        private_constraints.read_bytes() != constraints
        or regular_bytes(build_constraints) != constraints
    ):
        raise ReleaseRejectedError("build constraints changed during application build")
    receipt: dict[str, object] = identity | {
        "wheel": wheel.name,
        "wheel_digest": digest,
        "build_constraints_digest": hashlib.sha256(constraints).hexdigest(),
    }
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
    parser.add_argument("--build-constraints", type=Path, required=True)
    args = parser.parse_args()
    result = build_application(
        args.repo,
        args.commit,
        args.output,
        uv=args.uv,
        python=args.python,
        build_constraints=args.build_constraints,
        cache_dir=args.cache_dir,
    )
    print(json.dumps({"wheel": str(result.wheel), "wheel_digest": result.wheel_digest}))


if __name__ == "__main__":
    main()
