"""Config-free release verification and atomic generation selection.

Ordinary image startup and the finite release executor verify retained image
bytes here. The caller owns operation admission and independently establishes
platform/schema compatibility before selection; a verified image alone grants
no mutation authority. Generations are assembled at their final path because
venv entry-point shebangs are not relocatable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from shared.platform import file_lock
from shared.process_evidence import Digest, EvidenceModel

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


def _recorded_setuptools_helper(root: Path, path: Path) -> bool:
    """Accept the locked dependency's helper, not arbitrary executable .pth code.

    The wheel input and full installed inventory are separately hash-verified.
    Compare against a private copy of the original locked wheel, not mutable
    installed RECORD claims. Real isolated launch must still verify origins.
    """
    if path.name != "distutils-precedence.pth":
        return False
    wheel = root / "wheel-evidence/setuptools.whl"
    if not wheel.is_file():
        return False
    with zipfile.ZipFile(wheel) as archive:
        for relative in (path.name, "_distutils_hack/__init__.py"):
            member = path.parent / relative
            if archive.namelist().count(relative) != 1 or not member.is_file():
                return False
            if member.read_bytes() != archive.read(relative):
                return False
    return True


@dataclass(frozen=True)
class VerifiedRelease:
    """Absolute immutable paths captured once, never through a moving pointer."""

    digest: str
    manifest_digest: str
    root: Path
    interpreter: Path
    cwd: Path

    def module_argv(self, module: str, *arguments: str) -> tuple[str, ...]:
        """Construct isolated Python argv without a shell or moving pointer.

        Disable bytecode writes so importing a sealed image does not mutate its
        inventory. Isolation ignores cwd/PYTHONPATH; UTF-8 is explicit on Windows.
        The lifecycle owner still decides whether and when to spawn this argv.
        """
        if not module or not all(part.isidentifier() for part in module.split(".")):
            raise ReleaseRejectedError("invalid Python module entry point")
        return (str(self.interpreter), "-I", "-B", "-X", "utf8", "-m", module, *arguments)


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
    if store.resolve() != store.absolute() or root.is_symlink() or not root.is_dir():
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
    raw_files = cast(dict[object, object], manifest["files"])
    files = {_relative(name): _digest(value) for name, value in raw_files.items()}
    if "manifest.json" in files:
        raise ReleaseRejectedError("manifest cannot hash itself")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseRejectedError("release contains a symlink")
        if not (path.is_file() or path.is_dir()):
            raise ReleaseRejectedError("release contains a special file")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ReleaseRejectedError("release inventory differs from files on disk")
    for relative, expected_hash in files.items():
        path = _plain_file(root, relative)
        if file_sha256(path) != expected_hash:
            raise ReleaseRejectedError(f"release member hash mismatch: {relative}")
        if (
            path.suffix == ".pth"
            and path.parent.name in {"site-packages", "dist-packages"}
            and not _recorded_setuptools_helper(root, path)
        ):
            raise ReleaseRejectedError(
                f"release contains untrusted active path injection: {relative}"
            )
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
    if not isinstance(value, dict):
        raise ReleaseRejectedError("invalid release pointer")
    pointer_fields = cast(dict[object, object], value)
    if set(pointer_fields) != {"artifact_digest", "manifest_digest"}:
        raise ReleaseRejectedError("invalid release pointer")
    return _digest(pointer_fields["artifact_digest"]), _digest(pointer_fields["manifest_digest"])


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
    if not store.is_dir() or store.resolve() != store.absolute():
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
