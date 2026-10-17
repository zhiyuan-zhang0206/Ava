"""Pinned collector acquisition, independent of runtime settings and convergence."""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from base.clock import Timers
from base.host.net.ports import UrlOpener
from base.host.net.resilience import Policy, retry

# Pinned contrib version — re-validate against the deploy/lgtm backends
# (Tempo/Loki/Prometheus OTLP intake) when bumping.
OTELCOL_CONTRIB_VERSION = "0.162.0"

# SHA256 of each supported platform's release tarball
# (opentelemetry-collector-releases v0.162.0 checksums). Keyed by the platform
# tag used in the asset name.
_OTELCOL_CONTRIB_SHA256: dict[str, str] = {
    "darwin_arm64": "d5e11974d2e4adac3cc001a25137aad52f6e77ec3034ba7735cac7c219a5f97a",
    "darwin_amd64": "91a6e7a0f5e1981986c897db4535de5ba8d7fc45d4205809aecb9ebebf68c71c",
    "linux_amd64": "fcc063749f730f8c21fe29f2d340ff174f5f1c5885bd3156fb6c985a3036fcc3",
    "linux_arm64": "ecf6a917e2b53beb1703b19a84bc4e112a2fcaba549639cf6b0ba58446bfd7d2",
}

_DOWNLOAD_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/"
    f"releases/download/v{OTELCOL_CONTRIB_VERSION}/"
    "otelcol-contrib_{version}_{tag}.tar.gz"
)

VERSION_MARKER = "version"

# Download fetch discipline (issue #172): the tarball is on the critical path
# of `meiva start`, so a slow or dead mirror must fail the converge step in
# bounded time and say so — never stall bring-up indefinitely. Per-read socket
# timeout catches a wedged connection; a total wall-clock cap per attempt
# catches a mirror that trickles forever; a bounded retry rides out transient
# blips; a heartbeat line distinguishes slow from dead.
DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
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
    linux_arm64. Anything else (linux_386, ...)
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
    return None


def binary_name() -> str:
    return "otelcol-contrib"


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def stream_download(
    url: str,
    dest: Path,
    *,
    open_url: UrlOpener,
    timers: Timers,
    attempt_timeout_s: float = _DOWNLOAD_ATTEMPT_TIMEOUT_S,
) -> None:
    started = timers.monotonic()
    last_beat = started
    got = 0
    total: int | None = None

    def _check_budget() -> float:
        now = timers.monotonic()
        if now - started > attempt_timeout_s:
            raise TimeoutError(f"download exceeded {attempt_timeout_s:.0f}s wall-clock cap")
        return now

    with (
        open_url(url, DOWNLOAD_SOCKET_TIMEOUT_S) as resp,
        dest.open("wb") as fh,
    ):
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = None
        while True:
            _check_budget()
            chunk = resp.read1(1 << 16)
            now = _check_budget()
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if now - last_beat >= _DOWNLOAD_PROGRESS_INTERVAL_S:
                pct = f" ({got * 100 // total}%)" if total else ""
                _report(f"  · otel-collector: {got / 1e6:.1f} MB{pct} in {now - started:.0f}s")
                last_beat = now
    _report(
        f"  · otel-collector: downloaded {got / 1e6:.1f} MB in {timers.monotonic() - started:.0f}s"
    )


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def download_with_retry(url: str, tarball: Path, *, open_url: UrlOpener, timers: Timers) -> None:
    started = timers.monotonic()
    attempt = 0

    def _download_once() -> None:
        nonlocal attempt
        attempt += 1
        try:
            stream_download(url, tarball, open_url=open_url, timers=timers)
        except Exception as exc:  # URLError / TimeoutError / OSError
            elapsed = timers.monotonic() - started
            _report(
                f"  ! otel-collector: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} "
                f"failed after {elapsed:.0f}s: {exc}",
                error=True,
            )
            raise

    try:
        retry(_DOWNLOAD_POLICY, sleep=timers.sleep)(_download_once)
    except Exception as exc:
        raise RuntimeError(
            f"failed to download otel-collector from {url} after {_DOWNLOAD_ATTEMPTS} "
            f"attempts ({timers.monotonic() - started:.0f}s total): {exc}"
        ) from exc


def download_and_verify(tag: str, dest_dir: Path, *, open_url: UrlOpener, timers: Timers) -> None:
    """Download + SHA256-verify + extract the pinned tarball into dest_dir."""
    url = _DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag)
    expected = _OTELCOL_CONTRIB_SHA256[tag]
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        _report(f"  · otel-collector: downloading {url}")
        download_with_retry(url, tarball, timers=timers, open_url=open_url)
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
        dest.chmod(0o755)
    (dest_dir / VERSION_MARKER).write_text(OTELCOL_CONTRIB_VERSION + "\n", encoding="utf-8")


def _report(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")
    stream.flush()
