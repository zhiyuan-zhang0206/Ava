"""Pinned collector acquisition, independent of runtime settings and convergence."""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from shared.resilience import Policy, retry

# Pinned contrib version — re-validate against the deploy/lgtm backends
# (Tempo/Loki/Prometheus OTLP intake) when bumping.
OTELCOL_CONTRIB_VERSION = "0.157.0"

# SHA256 of each supported platform's release tarball
# (opentelemetry-collector-releases v0.157.0 checksums). Keyed by the platform
# tag used in the asset name.
_OTELCOL_CONTRIB_SHA256: dict[str, str] = {
    "darwin_arm64": "6c03308935573712a795b4229f756bc4288bbbb13850604f3c7287868af84d4b",
    "darwin_amd64": "e11e7482144c3ac1eb1f612d3d175589435cad968a791d6ef5c73be43e1b8c34",
    "linux_amd64": "d33177515a244a2393f03ffd66ab3e68a8fc11a56bc145ec4d0ca2644ee95504",
    "linux_arm64": "34eb82390c462c877dd60ec5ec84de899088916facd07306ec988e4c34bd05b3",
    "windows_amd64": "7b3938e1522ff04261a694a58e7111c5f7cdd19be617c3a77118cffad7abb815",
}

_DOWNLOAD_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/"
    f"releases/download/v{OTELCOL_CONTRIB_VERSION}/"
    "otelcol-contrib_{version}_{tag}.tar.gz"
)

VERSION_MARKER = "version"

# Download fetch discipline (issue #172): the tarball is on the critical path
# of `ava start`, so a slow or dead mirror must fail the converge step in
# bounded time and say so — never stall bring-up indefinitely. Per-read socket
# timeout catches a wedged connection; a total wall-clock cap per attempt
# catches a mirror that trickles forever; a bounded retry rides out transient
# blips; a heartbeat line distinguishes slow from dead.
_DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
_DOWNLOAD_ATTEMPT_TIMEOUT_S = 600.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5.0
_DOWNLOAD_PROGRESS_INTERVAL_S = 15.0


def _download_backoff(attempt: int) -> float:
    return _DOWNLOAD_RETRY_BACKOFF_S * (attempt + 1)


# Pinned collector download retries every failure on the original 5s, 10s schedule.
_DOWNLOAD_POLICY = Policy(
    max_attempts=_DOWNLOAD_ATTEMPTS,
    backoff=_download_backoff,
    jitter="none",
    jitter_span=1.0,
    classify=lambda _exc: True,
    idempotent=True,
    respect_retry_after=False,
    on_final_failure=None,
)


def platform_tag() -> str | None:
    """The release asset tag for this machine, or None when unsupported.

    Tags follow the release assets: darwin_arm64 / darwin_amd64 / linux_amd64 /
    linux_arm64 / windows_amd64. Anything else (linux_386, windows_arm64, ...)
    has no pinned binary — the sidecar is skipped and OTLP export auto-disables
    at the agent preflight.
    """
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if arch is None:
        return None
    if platform.system() == "Darwin":
        return f"darwin_{arch}"
    if platform.system() == "Linux":
        return f"linux_{arch}"
    if platform.system() == "Windows":
        return "windows_amd64" if arch == "amd64" else None
    return None


def binary_name() -> str:
    return "otelcol-contrib.exe" if platform.system() == "Windows" else "otelcol-contrib"


def _stream_download(url: str, dest: Path) -> None:
    """Stream ``url`` into ``dest`` with the issue #172 fetch discipline.

    A bounded, loud download: per-read socket timeout, a wall-clock cap on the
    whole attempt, a progress heartbeat every 15 s (so a slow mirror reads as
    "slow", not "hung"), and a RuntimeError naming the URL + elapsed time when
    the attempt exceeds its budget. Deliberately not ``urlretrieve`` — it has
    no timeout parameter, which is exactly the gap this fixes.
    """
    started = time.monotonic()
    last_beat = started
    got = 0
    total: int | None = None
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_SOCKET_TIMEOUT_S) as resp,  # noqa: S310 — pinned https release asset
        dest.open("wb") as fh,
    ):
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = None
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            now = time.monotonic()
            if now - last_beat >= _DOWNLOAD_PROGRESS_INTERVAL_S:
                pct = f" ({got * 100 // total}%)" if total else ""
                _report(f"  · otel-collector: {got / 1e6:.1f} MB{pct} in {now - started:.0f}s")
                last_beat = now
            if now - started > _DOWNLOAD_ATTEMPT_TIMEOUT_S:
                raise TimeoutError(
                    f"download exceeded {_DOWNLOAD_ATTEMPT_TIMEOUT_S:.0f}s wall-clock cap"
                )
    _report(
        f"  · otel-collector: downloaded {got / 1e6:.1f} MB in {time.monotonic() - started:.0f}s"
    )


def _download_with_retry(url: str, tarball: Path) -> None:
    """Bounded retry around ``_stream_download``; a final failure names the
    URL and total elapsed time so the operator knows exactly what to fix."""
    started = time.monotonic()
    attempt = 0

    def _download_once() -> None:
        nonlocal attempt
        attempt += 1
        try:
            _stream_download(url, tarball)
        except Exception as exc:  # URLError / TimeoutError / OSError
            elapsed = time.monotonic() - started
            _report(
                f"  ! otel-collector: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} "
                f"failed after {elapsed:.0f}s: {exc}",
                error=True,
            )
            raise

    try:
        retry(_DOWNLOAD_POLICY)(_download_once)
    except Exception as exc:
        raise RuntimeError(
            f"failed to download otel-collector from {url} after {_DOWNLOAD_ATTEMPTS} "
            f"attempts ({time.monotonic() - started:.0f}s total): {exc}"
        ) from exc


def download_and_verify(tag: str, dest_dir: Path) -> None:
    """Download + SHA256-verify + extract the pinned tarball into dest_dir."""
    url = _DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag)
    expected = _OTELCOL_CONTRIB_SHA256[tag]
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        _report(f"  · otel-collector: downloading {url}")
        _download_with_retry(url, tarball)
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(
                f"otelcol-contrib {OTELCOL_CONTRIB_VERSION} {tag} SHA256 mismatch: "
                f"got {digest}, expected {expected} — refusing to install"
            )
        with tarfile.open(tarball) as tf:
            members = [m for m in tf.getmembers() if m.name.endswith(binary_name())]
            if not members:
                raise RuntimeError(f"otelcol-contrib tarball has no {binary_name()} member")
            tf.extract(members[0], path=tmp)
        extracted = Path(tmp) / members[0].name
        dest = dest_dir / binary_name()
        shutil.move(str(extracted), dest)
        if platform.system() != "Windows":
            dest.chmod(0o755)
    (dest_dir / VERSION_MARKER).write_text(OTELCOL_CONTRIB_VERSION + "\n", encoding="utf-8")


def _report(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")
    stream.flush()
