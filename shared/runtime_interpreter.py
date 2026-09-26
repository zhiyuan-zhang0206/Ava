"""Interpreter paths for the currently imported code, never a moving selector.

Editable development keeps its checkout venv. A wheel consumes the interpreter
that loaded it, not an imaginary site-packages/.venv. Release verification and
activation remain the deployment owner's responsibility; this is not admission.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from shared.platform import IS_WINDOWS
from shared.process_evidence import Digest, EvidenceModel
from shared.runtime_release import (
    ApplicationIdentity,
    ReleaseRejectedError,
    VerifiedRelease,
    read_application_identity,
    verify_release,
)
from shared.verified_file import regular_bytes

_PREFIX = Path(sys.prefix).resolve()
WHEEL_RUNTIME = Path(__file__).resolve().is_relative_to(_PREFIX)


def runtime_venv(*, checkout: Path | None = None) -> Path:
    """Return the current runtime environment or an explicitly targeted checkout."""
    if checkout is not None:
        return checkout / ".venv"
    if WHEEL_RUNTIME:
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("installed Ava requires an isolated virtual environment")
        return _PREFIX
    from shared.paths import repo_root

    return repo_root() / ".venv"


def runtime_python() -> Path:
    """Absolute Python path anchored to the loaded wheel or development checkout."""
    return runtime_venv() / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def runtime_frontend_dir() -> Path:
    """Bind the frontend to the same loaded generation; admission verifies assets."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained frontend paths require wheel runtime")
    return runtime_venv().parent / "frontend"


def runtime_plugins_dir() -> Path:
    """Read-only external plugin root of the already loaded generation."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained plugin paths require wheel runtime")
    return runtime_venv().parent / "plugins"


def external_plugin_read_root() -> Path:
    """Shared discovery source; never use this as an installer destination."""
    if WHEEL_RUNTIME:
        return runtime_plugins_dir()
    from shared.paths import plugins_dir

    return plugins_dir()


def runtime_otel_binary() -> Path:
    """Resolve only the loaded image's collector, never mutable home storage."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained collector paths require wheel runtime")
    return (
        runtime_venv().parent
        / "otel"
        / ("otelcol-contrib.exe" if IS_WINDOWS else "otelcol-contrib")
    )


_STABLE_STAT = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


def source_digest(repo: Path) -> str:
    """Bind the names, modes and bytes Git would include, without changing Git."""
    head = subprocess.check_output(  # noqa: S603 — fixed read-only Git argv, canonical checkout
        ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30
    )
    result = subprocess.run(  # noqa: S603 — fixed read-only Git argv, no shell
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    names = sorted(set(result.stdout.split(b"\0")) - {b""})
    digest = hashlib.sha256(head)
    for encoded in names:
        path = repo / encoded.decode("utf-8")
        digest.update(encoded + b"\0")
        try:
            before = path.lstat()
            mode = before.st_mode
        except FileNotFoundError:
            digest.update(b"deleted\0")
            continue
        if stat.S_ISLNK(mode):
            content = str(path.readlink()).encode()
        elif stat.S_ISREG(mode):
            content = path.read_bytes()
        else:
            raise RuntimeError(f"unsupported development source member: {path}")
        after = path.lstat()
        if any(getattr(before, key) != getattr(after, key) for key in _STABLE_STAT):
            raise RuntimeError(f"development source changed while reading: {path}")
        digest.update(str(mode).encode() + b"\0" + hashlib.sha256(content).digest())
    if (
        subprocess.check_output(  # noqa: S603 — same read-only HEAD check
            ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30
        )
        != head
    ):
        raise RuntimeError("development HEAD changed while reading source")
    return digest.hexdigest()


class LoadedRuntimeIdentity(EvidenceModel):
    """Loaded code identity; no selector, publication or maintenance permission."""

    kind: Literal["source", "release"]
    code_root: str
    interpreter: str
    prefix: str
    cwd: str
    source_digest: Digest | None = None
    artifact_digest: Digest | None = None
    manifest_digest: Digest | None = None
    schema_digest: Digest | None = None
    source_commit: str | None = None

    @model_validator(mode="after")
    def explicit_origin(self) -> Self:
        for value in (self.code_root, self.interpreter, self.prefix, self.cwd):
            if not Path(value).is_absolute() or str(Path(value)) != value:
                raise ValueError("runtime paths must be explicit absolute paths")
        release = (
            self.artifact_digest,
            self.manifest_digest,
            self.schema_digest,
            self.source_commit,
        )
        if self.kind == "source":
            if self.source_digest is None or any(value is not None for value in release):
                raise ValueError("source runtime requires only its captured source digest")
        elif self.source_digest is not None or any(value is None for value in release):
            raise ValueError("release runtime requires complete captured artifact identity")
        return self


def loaded_runtime() -> tuple[Path, Path, Path, bool]:
    """Read actual imports without importing Settings or a higher application layer."""
    package = Path(__file__).resolve().parents[1]
    for name in ("shared", "cli", "ava", "agent", "gateway", "services"):
        module = sys.modules.get(name)
        if (
            module is not None
            and module.__file__ is not None
            and Path(module.__file__).resolve().parent.parent != package
        ):
            raise ReleaseRejectedError("loaded modules belong to different installations")
    return (
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve(),
        package,
        bool(sys.flags.isolated and sys.flags.dont_write_bytecode),
    )


def verify_loaded_source(checkout: Path) -> LoadedRuntimeIdentity:
    """Capture the exact developer checkout and the interpreter loading it."""
    prefix, executable, package, _isolated = loaded_runtime()
    if (
        checkout.resolve(strict=True) != checkout
        or package != checkout
        or package.is_relative_to(prefix)
    ):
        raise ReleaseRejectedError("development runtime differs from its loaded canonical checkout")
    return LoadedRuntimeIdentity(
        kind="source",
        code_root=str(package),
        interpreter=str(executable),
        prefix=str(prefix),
        cwd=str(checkout),
        source_digest=source_digest(checkout),
    )


def verify_loaded_image(
    home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
) -> LoadedRuntimeIdentity:
    """Verify immutable origin without granting start, selection, or migration."""
    import platform

    if not home.is_absolute() or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("release start requires a canonical existing home")
    if image.root != home / "releases" / image.digest:
        raise ReleaseRejectedError("release start image belongs to another home")
    verified = verify_release(
        image.root.parent,
        image.digest,
        manifest_digest=image.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    if verified != image:
        raise ReleaseRejectedError("captured release paths differ from verified inventory")
    prefix, executable, package, isolated = loaded_runtime()
    if (
        prefix != image.root / "venv"
        or executable != image.interpreter
        or not package.is_relative_to(prefix)
        or not isolated
    ):
        raise ReleaseRejectedError(
            "release start requires its actual isolated -I -B interpreter/modules"
        )
    read_application_identity(image, source_commit)
    return LoadedRuntimeIdentity(
        kind="release",
        code_root=str(package),
        interpreter=str(executable),
        prefix=str(prefix),
        cwd=str(image.cwd),
        artifact_digest=image.digest,
        manifest_digest=image.manifest_digest,
        schema_digest=schema_digest,
        source_commit=source_commit,
    )


def capture_loaded_runtime(home: Path) -> LoadedRuntimeIdentity:
    """Capture the current source or image, never infer it from a moving selector."""
    import platform

    prefix, _executable, package, _isolated = loaded_runtime()
    if not package.is_relative_to(prefix):
        return verify_loaded_source(package)
    identity = ApplicationIdentity.model_validate_json(
        regular_bytes(package / "shared" / "release-build.json")
    )
    root = prefix.parent
    image = verify_release(
        home / "releases",
        root.name,
        manifest_digest=hashlib.sha256(
            regular_bytes(root / "manifest.json", max_bytes=32 * 1024 * 1024)
        ).hexdigest(),
        platform_tag=platform.platform(),
        schema_digest=identity.schema_digest,
    )
    return verify_loaded_image(
        home, image, schema_digest=identity.schema_digest, source_commit=identity.source_commit
    )
