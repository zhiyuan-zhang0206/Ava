"""Dormant, config-free release verification and atomic generation selection.

No production launcher consumes this pointer yet. Callers must hold the official
rollout lease and supply independently observed platform/schema compatibility
before using activation. This module never installs dependencies, migrates a
database, stops services, or falls back to a checkout. Generations are assembled
at their final path (venv entry-point shebangs are not relocatable).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from shared.platform import file_lock

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseRejectedError(ValueError):
    """Unverified, incompatible, or stale activation intent."""


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ReleaseRejectedError("expected a lowercase SHA256 digest")
    return value


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseRejectedError("release path must be a nonempty string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in value
        or ":" in value
    ):
        raise ReleaseRejectedError(f"unsafe release path: {value!r}")
    return value


def _plain_file(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ReleaseRejectedError(f"release contains a symlink: {relative}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReleaseRejectedError(f"release member is not a private regular file: {relative}")
    return path


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class VerifiedRelease:
    """Absolute immutable paths captured once, never through a moving pointer."""

    digest: str
    manifest_digest: str
    root: Path
    interpreter: Path
    cwd: Path


def verify_release(
    store: Path,
    digest: str,
    *,
    manifest_digest: str,
    platform_tag: str,
    schema_digest: str,
) -> VerifiedRelease:
    """Hash the complete generation and reject unknown files or compatibility."""
    digest = _digest(digest)
    root = store / digest
    if store.is_symlink() or root.is_symlink() or not root.is_dir():
        raise ReleaseRejectedError("release store/generation must be a real directory")
    manifest_path = _plain_file(root, "manifest.json")
    if file_sha256(manifest_path) != _digest(manifest_digest):
        raise ReleaseRejectedError(
            "manifest digest does not match independently verified install record"
        )
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        "version",
        "artifact_digest",
        "platform",
        "schema_digest",
        "interpreter",
        "cwd",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected or manifest["version"] != 1:
        raise ReleaseRejectedError("unsupported release manifest shape/version")
    if manifest["artifact_digest"] != digest:
        raise ReleaseRejectedError("artifact identity differs from generation directory")
    if manifest["platform"] != platform_tag or _digest(manifest["schema_digest"]) != _digest(
        schema_digest
    ):
        raise ReleaseRejectedError("release platform/schema incompatible with observed host")
    if not isinstance(manifest["files"], dict) or not manifest["files"]:
        raise ReleaseRejectedError("release must declare a nonempty complete file inventory")
    files = {_relative(name): _digest(value) for name, value in manifest["files"].items()}
    if "manifest.json" in files:
        raise ReleaseRejectedError("manifest cannot hash itself")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseRejectedError("release contains a symlink")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ReleaseRejectedError("release inventory differs from files on disk")
    for relative, expected_hash in files.items():
        path = _plain_file(root, relative)
        if file_sha256(path) != expected_hash:
            raise ReleaseRejectedError(f"release member hash mismatch: {relative}")
        if path.suffix == ".pth":
            raise ReleaseRejectedError("release contains path injection metadata")
        if path.name == "direct_url.json":
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if metadata.get("dir_info", {}).get("editable"):
                raise ReleaseRejectedError("release contains an editable installation")
    interpreter_name = _relative(manifest["interpreter"])
    cwd_name = _relative(manifest["cwd"])
    if interpreter_name not in files:
        raise ReleaseRejectedError("interpreter is not included in verified inventory")
    cwd = root / cwd_name
    if not cwd.is_dir() or not any(name.startswith(cwd_name + "/") for name in files):
        raise ReleaseRejectedError("working directory has no verified runtime contents")
    return VerifiedRelease(
        digest,
        manifest_digest,
        root.absolute(),
        (root / interpreter_name).absolute(),
        cwd.absolute(),
    )


def current_pointer(store: Path) -> tuple[str, str] | None:
    """Read one complete pointer; never silently fall back to production source."""
    pointer = store / "current-release"
    if pointer.is_symlink():
        raise ReleaseRejectedError("current-release must not be a symlink")
    try:
        value = json.loads(pointer.read_text(encoding="ascii"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or set(value) != {"artifact_digest", "manifest_digest"}:
        raise ReleaseRejectedError("invalid release pointer")
    return _digest(value["artifact_digest"]), _digest(value["manifest_digest"])


def activate_release(
    store: Path,
    target: str,
    *,
    expected_current: tuple[str, str] | None,
    manifest_digest: str,
    platform_tag: str,
    schema_digest: str,
) -> VerifiedRelease:
    """CAS an atomic pointer after verification; does not restart any service.

    The stable advisory lock serializes cooperating writers across processes.
    A failed validation/replacement leaves the prior pointer intact. The caller
    retains the returned absolute paths for spawning; it must not execute via
    a pointer that can change again. No release deletion/GC is performed here.
    """
    if not store.is_dir() or store.is_symlink():
        raise ReleaseRejectedError("release store must already exist as a real directory")
    if expected_current is not None:
        for digest in expected_current:
            _digest(digest)
    if (store / "activation.lock").is_symlink():
        raise ReleaseRejectedError("activation lock must not be a symlink")
    with file_lock(store / "activation.lock", timeout_s=5):
        if current_pointer(store) != expected_current:
            raise ReleaseRejectedError("activation predecessor changed")
        release = verify_release(
            store,
            target,
            manifest_digest=manifest_digest,
            platform_tag=platform_tag,
            schema_digest=schema_digest,
        )
        fd, temporary = tempfile.mkstemp(prefix=".current-release-", dir=store)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                json.dump(
                    {"artifact_digest": release.digest, "manifest_digest": release.manifest_digest},
                    stream,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(store / "current-release")
        finally:
            Path(temporary).unlink(missing_ok=True)
    return release
