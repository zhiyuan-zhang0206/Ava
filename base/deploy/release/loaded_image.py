"""The retained-image half of loaded-runtime identity.

Everything here applies only to code loaded from an immutable release image
(`WHEEL_RUNTIME`). Source checkouts, the only production runtime, use
`base.deploy.release.runtime_interpreter` and never import this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.deploy.release.identity import read_application_identity
from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity, loaded_runtime
from base.deploy.release.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    verify_release,
)
from base.native_process.os_platform import IS_WINDOWS
from base.runtime_abi import current_abi

_PREFIX = Path(sys.prefix).resolve()
WHEEL_RUNTIME = Path(__file__).resolve().is_relative_to(_PREFIX)


def runtime_venv() -> Path:
    """The isolated environment prefix the loaded wheel runs from."""
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("installed Ava requires an isolated virtual environment")
    return _PREFIX


def runtime_python() -> Path:
    """Absolute Python path anchored to the loaded wheel."""
    return runtime_venv() / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def runtime_plugins_dir() -> Path:
    """Read-only external plugin root of the already loaded generation."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained plugin paths require wheel runtime")
    return runtime_venv().parent / "plugins"


def runtime_otel_binary() -> Path:
    """Resolve only the loaded image's collector, never mutable home storage."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained collector paths require wheel runtime")
    return (
        runtime_venv().parent
        / "otel"
        / ("otelcol-contrib.exe" if IS_WINDOWS else "otelcol-contrib")
    )


def verify_loaded_image(
    home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
) -> LoadedRuntimeIdentity:
    """Verify immutable origin without granting start, selection, or migration."""
    if not home.is_absolute() or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("release start requires a canonical existing home")
    if image.root != home / "releases" / image.digest:
        raise ReleaseRejectedError("release start image belongs to another home")
    verified = verify_release(
        image.root.parent,
        image.digest,
        manifest_digest=image.manifest_digest,
        host_abi=current_abi(),
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
