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

pgvector is injected into the extracted tree from the platform's package
channel (PGDG deb on Linux, Homebrew bottle on macOS) — see the pin table below.

Redis is vendored separately (a prebuilt we publish) — not here yet.
"""

from __future__ import annotations

import hashlib
import io
import lzma
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


def _download(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    import urllib.error
    import urllib.request

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 — pinned https artifact URLs
            with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
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


# ── pgvector injection ────────────────────────────────────────────────────
#
# zonky ships core + contrib only — no third-party extensions, pgvector
# included (checked across every zonky version: 0 vector entries). pgvector
# has no official prebuilt releases either, so the extension files come from
# the platform's package channel and are copied into the extracted tree:
# `lib/postgresql/` (pkglibdir) gets the loadable module, `share/postgresql/
# extension/` gets the control + install SQL. Verified layout facts (see
# future/infra/vendored-data-plane-binaries.md): `$libdir` resolves to
# `<prefix>/lib/postgresql`, and the macOS module suffix is `.dylib`.

# The pinned pgvector version. A PG major bump must re-pin these artifacts
# (pgvector builds are per-major: PGDG ships postgresql-<major>-pgvector,
# Homebrew bottles carry per-major dirs).
_PGVECTOR_VERSION = "0.8.6"
_PGVECTOR_SQL = f"vector--{_PGVECTOR_VERSION}.sql"

# platform key -> (download URL, pinned sha256 of the whole artifact).
# The deb is a plain HTTPS file; the bottle is a content-addressed ghcr.io
# blob (the sha256 Homebrew publishes in the formula), which needs an
# anonymous pull token — handled by _download_ghcr_blob.
_PGVECTOR_ARTIFACTS: dict[str, tuple[str, str]] = {
    "linux-x86_64": (
        "https://apt.postgresql.org/pub/repos/apt/pool/main/p/pgvector/"
        "postgresql-17-pgvector_0.8.6-1.pgdg12+1_amd64.deb",
        "76e6d5752dd2073f79b7b8d59c6d9a17996baf180aed3bfcbd38f6078b565295",
    ),
    # The zonky darwin jar is a universal binary, but pgvector bottles are
    # per-arch, so macOS splits on the machine arch.
    "darwin-arm64": (
        "https://ghcr.io/v2/homebrew/core/pgvector/blobs/"
        "sha256:4163c0f061e78cb15e459d4c39979ec97037f45a7818f3d937008863f93358ba",
        "4163c0f061e78cb15e459d4c39979ec97037f45a7818f3d937008863f93358ba",
    ),
    "darwin-x86_64": (
        "https://ghcr.io/v2/homebrew/core/pgvector/blobs/"
        "sha256:a85fa44ed8ce583beff8e90c57cb87941b194814aa282714575be616ee113df2",
        "a85fa44ed8ce583beff8e90c57cb87941b194814aa282714575be616ee113df2",
    ),
}


def _pgvector_platform_key() -> str:
    """The pgvector artifact key. linux/arm64 is deliberately out of matrix
    (the PG pin table does not cover it either) — it raises instead of
    pretending support."""
    system = platform.system()
    if system == "Darwin":
        machine = platform.machine()
        if machine == "arm64":
            return "darwin-arm64"
        if machine in ("x86_64", "amd64"):
            return "darwin-x86_64"
        raise RuntimeError(f"no vendored pgvector available for darwin/{machine}")
    if system == "Linux":
        machine = platform.machine()
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        raise RuntimeError(f"no vendored pgvector available for linux/{machine}")
    raise RuntimeError(f"no vendored pgvector available for {system}")


def ensure_pgvector() -> None:
    """Download + inject the pinned pgvector extension files into the vendored
    Postgres tree (idempotent; fail-fast on download/checksum failure — the
    same contract as `ensure_pg_binaries`). The injected files are exactly
    what `CREATE EXTENSION vector` needs: the loadable module into
    `lib/postgresql/` and the control + install SQL into
    `share/postgresql/extension/`.

    Called by converge right after `ensure_pg_binaries()`, and by the CI smoke
    gate. Detection is the version-named install SQL file, so a future
    re-pin re-injects instead of silently leaving the old files in place.
    """
    pg_dir = vendored_pg_dir()
    if (pg_dir / "share/postgresql/extension" / _PGVECTOR_SQL).exists():
        return
    if not (pg_dir / "bin" / "initdb").exists():
        raise RuntimeError(
            f"vendored Postgres tree missing at {pg_dir} — ensure_pg_binaries() must run first"
        )
    key = _pgvector_platform_key()
    url, expected_sha = _PGVECTOR_ARTIFACTS[key]
    logger.info(f"[runtime] fetching pgvector {_PGVECTOR_VERSION} ({key}) from {url}")
    blob = _download_ghcr_blob(url) if url.startswith("https://ghcr.io/") else _download(url)
    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"pgvector artifact sha256 mismatch for {key}: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    for relpath, content in _pgvector_extension_files(blob, key).items():
        target = pg_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        # Per-file tmp + atomic replace: a crash mid-injection cannot leave a
        # truncated module behind (and the version-SQL detection re-runs the
        # whole injection on the next converge either way).
        staging = target.with_name(f".{target.name}.tmp")
        staging.write_bytes(content)
        staging.replace(target)
    logger.info(f"[runtime] pgvector {_PGVECTOR_VERSION} injected into {pg_dir}")


def _pgvector_extension_files(blob: bytes, key: str) -> dict[str, bytes]:
    """The extension files from the pinned artifact, keyed by their target path
    inside the vendored PG tree. Fail-fast on a missing member: a renamed
    upstream layout must be re-pinned, never silently skipped."""
    major = _PG_VERSION.split(".", 1)[0]
    if key.startswith("darwin"):
        return _pgvector_files_from_bottle(blob, major)
    return _pgvector_files_from_deb(blob, major)


def _tar_member_bytes(tar: tarfile.TarFile, name: str) -> bytes:
    """Read one regular member by exact name. Deb data tars carry `./` member
    prefixes (dpkg-deb layout), so both the bare and the `./`-prefixed
    spelling are accepted; a member present under neither raises (see
    `_pgvector_extension_files`)."""
    member = None
    for candidate in (name, f"./{name}"):
        try:
            member = tar.getmember(candidate)
            break
        except KeyError:
            continue
    if member is None:
        raise RuntimeError(f"pgvector artifact member missing: {name}")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise RuntimeError(f"pgvector artifact member is not a regular file: {name}")
    return extracted.read()


def _pgvector_files_from_bottle(blob: bytes, major: str) -> dict[str, bytes]:
    """Extract the PG-`major` files from the Homebrew bottle (a gzipped tar whose
    members live under `pgvector/<version>/`). The bottle carries builds for
    every formula dependency major (postgresql@17 + postgresql@18 today); only
    the pinned major is taken."""
    prefix = f"pgvector/{_PGVECTOR_VERSION}/"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return {
            "lib/postgresql/vector.dylib": _tar_member_bytes(
                tar, f"{prefix}lib/postgresql@{major}/vector.dylib"
            ),
            "share/postgresql/extension/vector.control": _tar_member_bytes(
                tar, f"{prefix}share/postgresql@{major}/extension/vector.control"
            ),
            f"share/postgresql/extension/{_PGVECTOR_SQL}": _tar_member_bytes(
                tar, f"{prefix}share/postgresql@{major}/extension/{_PGVECTOR_SQL}"
            ),
        }


def _pgvector_files_from_deb(blob: bytes, major: str) -> dict[str, bytes]:
    """Extract the PG-`major` files from the PGDG deb. The deb is an `ar` archive
    wrapping `data.tar.xz`; the inner tar holds the install layout
    (`usr/lib/postgresql/<major>/lib/` + `usr/share/postgresql/<major>/extension/`).
    Pure stdlib on purpose — no `ar`/`dpkg` binary on the host is assumed."""
    data_member = _deb_data_member(blob)
    if not data_member.startswith(b"\xfd\x37\x7a\x58\x5a\x00"):
        raise RuntimeError(
            "pgvector deb data member is not xz-compressed — the artifact layout "
            "changed; re-pin the deb and its extraction"
        )
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data_member)), mode="r:") as tar:
        return {
            "lib/postgresql/vector.so": _tar_member_bytes(
                tar, f"usr/lib/postgresql/{major}/lib/vector.so"
            ),
            "share/postgresql/extension/vector.control": _tar_member_bytes(
                tar, f"usr/share/postgresql/{major}/extension/vector.control"
            ),
            f"share/postgresql/extension/{_PGVECTOR_SQL}": _tar_member_bytes(
                tar, f"usr/share/postgresql/{major}/extension/{_PGVECTOR_SQL}"
            ),
        }


def _deb_data_member(deb: bytes) -> bytes:
    """The `data.tar.*` member of a deb (an `ar` archive), parsed in pure
    stdlib: 8-byte `!<arch>` magic, then 60-byte member headers with a 16-byte
    name field and a 10-byte size field."""
    if not deb.startswith(b"!<arch>\n"):
        raise RuntimeError("pgvector artifact is not an ar archive (deb)")
    pos = 8
    while pos + 60 <= len(deb):
        name = deb[pos : pos + 16].decode("ascii", "replace").strip().rstrip("/")
        size_field = deb[pos + 48 : pos + 58].decode("ascii", "replace").strip()
        if not size_field.isdigit():
            raise RuntimeError("corrupt ar member header in the pgvector deb")
        size = int(size_field)
        member = deb[pos + 60 : pos + 60 + size]
        if name in ("data.tar.xz", "data.tar.zst"):
            return member
        pos += 60 + size + (size % 2)  # members are 2-byte aligned
    raise RuntimeError("no data.tar member in the pgvector deb")


def _download_ghcr_blob(url: str) -> bytes:
    """Fetch a ghcr.io blob. ghcr requires an anonymous pull token even for
    public blobs: the token endpoint issues a short-lived bearer for the
    repository, and the blob URL then answers with it."""
    import json

    repository = url[len("https://ghcr.io/v2/") :].split("/blobs/", 1)[0]
    token_json = _download(
        f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull"
    )
    token = json.loads(token_json).get("token")
    if not token:
        raise RuntimeError(f"ghcr.io token endpoint answered without a token for {repository}")
    return _download(url, headers={"Authorization": f"Bearer {token}"})
