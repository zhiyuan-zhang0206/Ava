"""Vendored relocatable data-plane binaries.

Ava fetches a relocatable Postgres distribution itself, so a clean machine does not
need `brew install postgresql@17` (or the apt equivalent) before running. The
binaries live host-level under `~/.ava/runtime/` — shared by every cluster and
checkout (they are read-only; only the *data* is per-cluster) — and
`shared.pg_tools.pg_tool()` prefers them over a brew/apt install, falling back to
brew/apt when the vendored tree is absent (so existing dev boxes are unaffected
until they next converge).

Postgres comes from zonky's `embedded-postgres-binaries` (Maven Central): a
purpose-built relocatable distribution that links its own libs via
`@loader_path/../lib` (macOS) / `$ORIGIN/../lib` (Linux), so `bin/ lib/ share/`
runs from any path — unlike a brew keg, which hardcodes absolute lib paths. The
Maven artifact is a `.jar` (a zip) wrapping one `postgres-<platform>.txz`.

Redis is vendored separately (a prebuilt we publish) — not here yet.
"""

from __future__ import annotations

import hashlib
import io
import platform
import shutil
import tarfile
import time
from pathlib import Path

from shared.config import settings
from shared.log import logger

# Pinned Postgres distribution. A major-version bump is an expand step (a new
# version dir beside the old + re-initdb / pg_upgrade), never an in-place swap —
# initdb and the data dir it created must share a major.
_PG_VERSION = "17.4.0"

_MAVEN_BASE = "https://repo1.maven.org/maven2/io/zonky/test/postgres"

# platform key -> (maven artifact id, pinned jar sha256). The darwin artifact is a
# universal binary (x86_64 + arm64), so one entry covers both Mac architectures.
_PG_ARTIFACTS: dict[str, tuple[str, str]] = {
    "darwin": (
        "embedded-postgres-binaries-darwin-arm64v8",
        "686fb3585077fcbb8b894305fda2b2278552a0a1c497ce53d9373b7c524b615e",
    ),
    "linux-x86_64": (
        "embedded-postgres-binaries-linux-amd64",
        "d9d216d3c1c119ad31b8a8de60b3cf2826516f711a04d3745ef4f1913f21a938",
    ),
}


def _platform_key() -> str:
    system = platform.system()
    if system == "Darwin":
        return "darwin"  # universal binary covers arm64 + x86_64
    if system == "Linux":
        machine = platform.machine()
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        raise RuntimeError(f"no vendored Postgres available for linux/{machine}")
    raise RuntimeError(f"no vendored Postgres available for {system}")


def runtime_root() -> Path:
    """Host-level binaries root, beside the cluster registry (independent of any one
    `$AVA_HOME`), so a single download serves every cluster + checkout on the box."""
    return Path(settings.general.cluster_registry).expanduser().parent / "runtime"


def vendored_pg_dir() -> Path:
    return runtime_root() / "pg" / _PG_VERSION


def vendored_pg_bin_dir() -> Path | None:
    """The vendored Postgres `bin/` if present + initialized, else None (the caller
    falls back to a brew/apt install). Pure resolution — never downloads."""
    bin_dir = vendored_pg_dir() / "bin"
    return bin_dir if (bin_dir / "initdb").exists() else None


def ensure_pg_binaries() -> Path:
    """Download + extract the relocatable Postgres into `vendored_pg_dir()` if it is
    not already there (idempotent). Returns the `bin/` dir. Called by converge /
    install — never on the resolution path. Fails fast on a download or checksum
    mismatch rather than leaving a half-present tree.

    Raises:
        RuntimeError: the platform has no vendored artifact, the download failed, or
            the jar's sha256 did not match the pin.
    """
    bin_dir = vendored_pg_dir() / "bin"
    if (bin_dir / "initdb").exists():
        return bin_dir

    key = _platform_key()
    artifact, expected_sha = _PG_ARTIFACTS[key]
    url = f"{_MAVEN_BASE}/{artifact}/{_PG_VERSION}/{artifact}-{_PG_VERSION}.jar"
    logger.info(f"[runtime] fetching vendored Postgres {_PG_VERSION} ({key}) from {url}")
    jar_bytes = _download(url)

    actual_sha = hashlib.sha256(jar_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"vendored Postgres jar sha256 mismatch for {artifact}-{_PG_VERSION}: "
            f"expected {expected_sha}, got {actual_sha}"
        )

    _extract_pg(jar_bytes, vendored_pg_dir())
    logger.info(f"[runtime] vendored Postgres {_PG_VERSION} ready at {vendored_pg_dir()}")
    return bin_dir


# Maven Central rate-limits bursts (HTTP 429); with the jar sha256-pinned, a bounded
# backoff on transient answers is safe. Any other 4xx is a permanent answer about the
# pinned artifact and still fails fast.
_DOWNLOAD_ATTEMPTS = 4
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


def _download(url: str) -> bytes:
    import urllib.error
    import urllib.request

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 — pinned https Maven Central URL
                return resp.read()
        except urllib.error.URLError as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            transient = status is None or status in _TRANSIENT_HTTP
            if not transient or attempt == _DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"failed to download vendored Postgres from {url}: {exc}"
                ) from exc
            delay = 2**attempt
            logger.warning(
                f"[runtime] transient error fetching {url} ({exc}); "
                f"retry {attempt}/{_DOWNLOAD_ATTEMPTS - 1} in {delay}s"
            )
            time.sleep(delay)
    raise AssertionError("unreachable: the last attempt either returned or raised")


def _extract_pg(jar_bytes: bytes, target: Path) -> None:
    """Extract the single `postgres-*.txz` inside the jar (a zip) into `target`,
    atomically: a fresh tree is built beside `target` and renamed in, so a crash
    mid-extract never leaves a half-tree that `vendored_pg_bin_dir()` would accept."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
        txz_names = [n for n in jar.namelist() if n.endswith(".txz")]
        if len(txz_names) != 1:
            raise RuntimeError(f"expected exactly one .txz in the Postgres jar, found {txz_names}")
        txz_bytes = jar.read(txz_names[0])

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(txz_bytes), mode="r:xz") as tar:
        # filter="data" is the safe extraction filter (rejects absolute paths /
        # traversal). The archive is checksum-pinned, so this is free defense.
        tar.extractall(staging, filter="data")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
