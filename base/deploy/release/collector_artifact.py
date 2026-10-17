"""Pinned collector acquisition, independent of runtime settings and convergence."""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DownloadLimits:
    total_budget_s: float
    socket_timeout_s: float
    attempts: int
    retry_backoff_s: float
    progress_interval_s: float


@dataclass
class DownloadProgress:
    received: int = 0


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def stream_download(
    url: str,
    dest: Path,
    *,
    open_url: UrlOpener,
    timers: Timers,
    limits: DownloadLimits,
    deadline: float,
    progress: DownloadProgress,
) -> None:
    def _remaining() -> float:
        return deadline - timers.monotonic()

    def _elapsed() -> float:
        return limits.total_budget_s - _remaining()

    def _require_budget() -> float:
        remaining = _remaining()
        if remaining <= 0:
            raise TimeoutError(
                f"otel-collector download exceeded its {limits.total_budget_s:.0f}s budget "
                f"({progress.received} bytes received, "
                f"{max(-remaining, 0.0):.1f}s past the deadline)"
            )
        return remaining

    last_beat = 0.0
    total: int | None = None
    with (
        open_url(url, min(limits.socket_timeout_s, _require_budget())) as resp,
        dest.open("wb") as fh,
    ):
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = None
        while True:
            _require_budget()
            chunk = resp.read1(1 << 16)
            _require_budget()
            if not chunk:
                break
            fh.write(chunk)
            progress.received += len(chunk)
            elapsed = _elapsed()
            if elapsed - last_beat >= limits.progress_interval_s:
                pct = f" ({progress.received * 100 // total}%)" if total else ""
                _report(
                    f"  · otel-collector: {progress.received / 1e6:.1f} MB{pct} in {elapsed:.0f}s"
                )
                last_beat = elapsed
    _report(f"  · otel-collector: downloaded {progress.received / 1e6:.1f} MB in {_elapsed():.0f}s")


def _download_policy(limits: DownloadLimits) -> Policy:
    def _backoff(attempt: int) -> float:
        return limits.retry_backoff_s * (attempt + 1)

    return Policy(
        max_attempts=limits.attempts,
        backoff=_backoff,
        jitter="none",
        jitter_span=1.0,
        classify=lambda _exc: True,
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )


# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
def download_with_retry(
    url: str, tarball: Path, *, open_url: UrlOpener, timers: Timers, limits: DownloadLimits
) -> None:
    started = timers.monotonic()
    deadline = started + limits.total_budget_s
    attempt = 0
    progress = DownloadProgress()

    def _used() -> float:
        return timers.monotonic() - started

    def _remaining() -> float:
        return deadline - timers.monotonic()

    def _wait(seconds: float) -> None:
        remaining = _remaining()
        if remaining > 0:
            timers.sleep(min(seconds, remaining))

    def _download_once() -> None:
        nonlocal attempt, progress
        attempt += 1
        progress = DownloadProgress()
        try:
            stream_download(
                url,
                tarball,
                open_url=open_url,
                timers=timers,
                limits=limits,
                deadline=deadline,
                progress=progress,
            )
        except Exception as exc:  # URLError / TimeoutError / OSError
            _report(
                f"  ! otel-collector: download attempt {attempt}/{limits.attempts} failed after "
                f"{_used():.0f}s ({max(_remaining(), 0.0):.0f}s of the "
                f"{limits.total_budget_s:.0f}s budget left, {progress.received} bytes received): "
                f"{exc}",
                error=True,
            )
            raise

    try:
        retry(_download_policy(limits), sleep=_wait)(_download_once)
    except Exception as exc:
        raise RuntimeError(
            f"failed to download otel-collector from {url} after {attempt} attempt(s) "
            f"({_used():.0f}s used of the {limits.total_budget_s:.0f}s budget, "
            f"{max(_remaining(), 0.0):.0f}s left, {progress.received} bytes received by the last "
            f"attempt): {exc}"
        ) from exc


def download_and_verify(
    tag: str, dest_dir: Path, *, open_url: UrlOpener, timers: Timers, limits: DownloadLimits
) -> None:
    """Download + SHA256-verify + extract the pinned tarball into dest_dir."""
    url = _DOWNLOAD_URL.format(version=OTELCOL_CONTRIB_VERSION, tag=tag)
    expected = _OTELCOL_CONTRIB_SHA256[tag]
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "otelcol-contrib.tar.gz"
        _report(f"  · otel-collector: downloading {url}")
        download_with_retry(url, tarball, timers=timers, open_url=open_url, limits=limits)
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
