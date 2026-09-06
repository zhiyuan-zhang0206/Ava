"""Read-only verification for registered private production files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

_DEFAULT_MANIFEST = Path(__file__).with_name("private-files") / "manifest.json"
_HASH_CHUNK_SIZE = 1024 * 1024


class GitSource(TypedDict):
    kind: Literal["git"]
    repo: str
    treeish: str


class ManifestEntry(TypedDict):
    id: str
    path: str
    source: GitSource
    sha256: str
    status: str
    notes: str


class RegistryManifest(TypedDict):
    version: int
    entries: list[ManifestEntry]


class OutcomeJson(TypedDict):
    id: str
    path: str
    ok: bool
    errors: list[str]


@dataclass(frozen=True)
class Outcome:
    """Verification result for one manifest entry."""

    entry_id: str
    path: str
    ok: bool
    errors: tuple[str, ...]

    def as_json(self) -> OutcomeJson:
        return {
            "id": self.entry_id,
            "path": self.path,
            "ok": self.ok,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class CliArgs:
    root: Path
    manifest: Path
    json_output: bool


def _load_manifest(path: Path) -> RegistryManifest:
    raw: object = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("private-files manifest must be a JSON object")
    manifest = cast(RegistryManifest, raw)
    if manifest["version"] != 1:
        raise ValueError(f"unsupported private-files manifest version: {manifest['version']!r}")
    if not isinstance(manifest["entries"], list):
        raise TypeError("private-files manifest entries must be a list")
    return manifest


def _relative_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"private-files path must be relative and stay under root: {raw_path!r}")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _git_source_errors(
    source: GitSource, relative_path: PurePosixPath, expected_sha256: str
) -> list[str]:
    if source["kind"] != "git":
        raise ValueError(f"unsupported private-files source kind: {source['kind']!r}")
    repo = Path(source["repo"]).expanduser()
    if not repo.is_dir():
        return [f"source git repository is missing: {repo}"]

    object_name = f"{source['treeish']}:{relative_path.as_posix()}"
    exists = subprocess.run(  # noqa: S603 — fixed git plumbing with list-form arguments.
        ["git", "-C", str(repo), "cat-file", "-e", object_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        return [f"source git object is missing: {repo}@{object_name}"]

    blob = subprocess.run(  # noqa: S603 — fixed git plumbing with list-form arguments.
        ["git", "-C", str(repo), "cat-file", "blob", object_name],
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0:
        return [f"source git object cannot be read: {repo}@{object_name}"]
    source_sha256 = hashlib.sha256(blob.stdout).hexdigest()
    if source_sha256 != expected_sha256:
        return [f"source sha256 mismatch: expected {expected_sha256}, got {source_sha256}"]
    return []


def verify(root: Path, manifest: Path) -> list[Outcome]:
    """Verify checked files and their durable git sources without writing either."""
    registry = _load_manifest(manifest)
    checked_root = root.expanduser()
    outcomes: list[Outcome] = []
    for entry in registry["entries"]:
        relative_path = _relative_path(entry["path"])
        expected_sha256 = entry["sha256"]
        errors: list[str] = []
        checked_file = checked_root.joinpath(*relative_path.parts)
        if not checked_file.is_file():
            errors.append("file is missing")
        else:
            actual_sha256 = _file_sha256(checked_file)
            if actual_sha256 != expected_sha256:
                errors.append(
                    f"file sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
                )
        errors.extend(_git_source_errors(entry["source"], relative_path, expected_sha256))
        outcomes.append(
            Outcome(
                entry_id=entry["id"],
                path=relative_path.as_posix(),
                ok=not errors,
                errors=tuple(errors),
            )
        )
    return outcomes


def _default_root() -> Path:
    ava_home = os.environ.get("AVA_HOME")
    return Path(ava_home).expanduser() / "source" if ava_home else Path.cwd()


def _parse_args(argv: Sequence[str] | None) -> CliArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="verify every registered file")
    verify_parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="checked tree root (default: $AVA_HOME/source, or cwd when AVA_HOME is unset)",
    )
    verify_parser.add_argument(
        "--manifest",
        type=Path,
        default=_DEFAULT_MANIFEST,
        help=f"registry manifest (default: {_DEFAULT_MANIFEST})",
    )
    verify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="emit machine-readable output"
    )
    parsed = parser.parse_args(argv)
    if cast(str, parsed.command) != "verify":
        raise AssertionError(f"unknown private-files command: {parsed.command!r}")
    root = cast(Path | None, parsed.root)
    return CliArgs(
        root=_default_root() if root is None else root,
        manifest=cast(Path, parsed.manifest),
        json_output=cast(bool, parsed.json_output),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    outcomes = verify(args.root, args.manifest)
    if args.json_output:
        sys.stdout.write(json.dumps([outcome.as_json() for outcome in outcomes]) + "\n")
    else:
        for outcome in outcomes:
            if outcome.ok:
                sys.stdout.write(f"OK {outcome.entry_id} {outcome.path}\n")
            else:
                sys.stdout.write(
                    f"FAIL {outcome.entry_id} {outcome.path}: {'; '.join(outcome.errors)}\n"
                )
    return 0 if all(outcome.ok for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
