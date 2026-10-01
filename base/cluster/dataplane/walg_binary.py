"""The pinned WAL-G binary, installed at one stable path under `$AVA_HOME/runtime/walg/`.

WAL-G is the physical-backup engine (`services/gateway_side/walg/`). Like the
vendored Postgres in `runtime_binaries`, Ava fetches it itself: a SHA-256-pinned
GitHub release asset, verified before it can replace anything. The path is
version-free (`wal-g`), so Postgres' `archive_command` can name it once and an
upgrade is an atomic file replacement that needs no Postgres restart.

Only Linux x86_64 has a pinned artifact: upstream publishes Linux builds only
(Ubuntu x amd64/aarch64), and the one that was proven in the restore drill is the
Ubuntu 24.04 amd64 build. Any other platform fails fast when the feature is
switched on rather than pretending support.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from base.cluster.dataplane.runtime_binaries import _download, runtime_root
from base.log import logger
from base.native_process.child_env import daemon_process_env

WALG_VERSION = "v3.0.9"

_RELEASE_BASE = "https://github.com/wal-g/wal-g/releases/download"

# platform key -> (release asset, pinned sha256 of the asset). The asset is the bare
# executable (61,330,280 bytes), checked against the digest the release publishes.
_WALG_ARTIFACTS: dict[str, tuple[str, str]] = {
    "linux-x86_64": (
        "wal-g-pg-24.04-amd64",
        "8015836f246a978f27b31a2462eac7765312558ae5b11ac033322b555abf9e22",
    ),
}

_VERSION_PROBE_TIMEOUT_S = 30


def _artifact() -> tuple[str, str]:
    system, machine = platform.system(), platform.machine()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return _WALG_ARTIFACTS["linux-x86_64"]
    raise RuntimeError(f"no pinned wal-g artifact for {system.lower()}/{machine}")


def walg_path() -> Path:
    """The stable installed path, `$AVA_HOME/runtime/walg/wal-g`. Pure — never downloads."""
    return runtime_root() / "walg" / "wal-g"


def _sha256_of(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as binary:
            while block := binary.read(1 << 20):
                digest.update(block)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def installed_problem() -> str | None:
    """Why the installed binary is not the pinned build, or None when it is.

    Judged by content hash, not by name or by running it: a binary somebody
    replaced by hand is a different program under the same path.
    """
    try:
        _asset, expected = _artifact()
    except RuntimeError as exc:
        return str(exc)
    path = walg_path()
    actual = _sha256_of(path)
    if actual is None:
        return f"wal-g is not installed at {path}"
    if actual != expected:
        return (
            f"wal-g at {path} is not the pinned {WALG_VERSION} build "
            f"(sha256 {actual[:12]}, pinned {expected[:12]})"
        )
    if not os.access(path, os.X_OK):
        return f"wal-g at {path} is not executable"
    return None


def _require_version(binary: Path) -> None:
    """The candidate binary must run and report the pinned version."""
    result = subprocess.run(  # noqa: S603 — the candidate is the checksum-verified pinned artifact
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_VERSION_PROBE_TIMEOUT_S,
        env=daemon_process_env(),
    )
    if result.returncode != 0 or WALG_VERSION not in result.stdout:
        raise RuntimeError(
            f"downloaded wal-g does not report {WALG_VERSION} "
            f"(exit {result.returncode}: {result.stdout.strip()[:120]!r})"
        )


def ensure_walg_binary() -> Path:
    """Install the pinned WAL-G at `walg_path()` if it is not already there (idempotent).

    The download is checked against the pinned SHA-256 and run once (`--version`)
    before it replaces the installed file, so a corrupt or wrong artifact never
    displaces a working binary. Called by converge — never on a resolution path.

    Raises:
        RuntimeError: the platform has no pinned artifact, the download failed,
            the SHA-256 did not match, or the binary did not report the pinned version.
    """
    asset, expected = _artifact()
    target = walg_path()
    if installed_problem() is None:
        return target

    url = f"{_RELEASE_BASE}/{WALG_VERSION}/{asset}"
    logger.info(f"[runtime] fetching wal-g {WALG_VERSION} from {url}")
    blob = _download(url, what="wal-g")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"wal-g artifact sha256 mismatch for {asset} {WALG_VERSION}: "
            f"expected {expected}, got {actual}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=target.parent, prefix=".wal-g.", suffix=".tmp")
    candidate = Path(staged)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(blob)
            file.flush()
            os.fsync(file.fileno())
        candidate.chmod(0o755)
        _require_version(candidate)
        candidate.replace(target)
    finally:
        with suppress(FileNotFoundError):
            candidate.unlink()
    logger.info(f"[runtime] wal-g {WALG_VERSION} ready at {target}")
    return target
