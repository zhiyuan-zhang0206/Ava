"""Native Loki and Prometheus installation and launchd configuration.

Converge downloads only a missing or stale release asset, while always
rendering the live configs and launchd plists so checkout changes apply on the
next lifecycle run.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from cli.commands._converge_spec import ConvergeCtx
from shared.log import logger

SUPPORTED_TAGS = {"darwin_arm64"}

_DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
_DOWNLOAD_ATTEMPT_TIMEOUT_S = 600.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5.0
_DOWNLOAD_PROGRESS_INTERVAL_S = 15.0
_LAUNCHCTL_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class _NativeService:
    arguments: tuple[str, ...]
    gomemlimit: str
    archive_member: str


_NATIVE_CONSTANTS: dict[str, _NativeService] = {
    "loki": _NativeService(
        arguments=("-config.file={config}/loki.yaml",),
        gomemlimit="2GiB",
        archive_member="loki-darwin-arm64",
    ),
    "prometheus": _NativeService(
        arguments=(
            "--config.file={config}/prometheus.yml",
            "--storage.tsdb.path={data}/prom",
            "--storage.tsdb.retention.time=90d",
            "--storage.tsdb.retention.size=8GB",
            "--web.enable-otlp-receiver",
            "--web.listen-address=127.0.0.1:9090",
        ),
        gomemlimit="1GiB",
        archive_member="prometheus-3.13.2.darwin-arm64/prometheus",
    ),
}


def platform_tag() -> str | None:
    """Return the pinned release tag for this machine, if native LGTM supports it."""
    if platform.system() != "Darwin":
        return None
    return "darwin_arm64" if platform.machine().lower() in {"arm64", "aarch64"} else None


def _load_versions(repo: Path) -> dict[str, dict[str, str]]:
    """Load the pinned version and release asset for every native backend."""
    raw = yaml.safe_load((repo / "deploy/lgtm/native/versions.yml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("deploy/lgtm/native/versions.yml must contain a mapping")
    entries = cast(dict[str, object], raw)
    versions: dict[str, dict[str, str]] = {}
    for name in _NATIVE_CONSTANTS:
        service = entries[name]
        if not isinstance(service, dict):
            raise TypeError(f"native LGTM version entry for {name} must be a mapping")
        values = cast(dict[str, object], service)
        version = values["version"]
        assets = values["assets"]
        if not isinstance(version, str) or not isinstance(assets, dict):
            raise TypeError(f"native LGTM version entry for {name} is invalid")
        asset = cast(dict[str, object], assets)["darwin-arm64"]
        if not isinstance(asset, dict):
            raise TypeError(f"native LGTM darwin-arm64 asset for {name} must be a mapping")
        asset_values = cast(dict[str, object], asset)
        url = asset_values["url"]
        sha256 = asset_values["sha256"]
        if not isinstance(url, str) or not isinstance(sha256, str):
            raise TypeError(f"native LGTM darwin-arm64 asset for {name} is invalid")
        versions[name] = {"version": version, "url": url, "sha256": sha256}
    return versions


def _plist_path(label: str) -> Path:
    """The launchd plist path for a native backend label."""
    return _agents_dir() / f"{label}.plist"


def _agents_dir() -> Path:
    """The user LaunchAgents directory used by native LGTM services."""
    return Path.home() / "Library" / "LaunchAgents"


def native_label(name: str, ava_home: Path) -> str:
    """Return the per-cluster launchd label for one native LGTM backend."""
    from shared.cluster import home_slug

    return f"com.ava.{name}.{home_slug(ava_home)}"


def _render_plist(name: str, native_dir: Path, ava_home: Path) -> str:
    """Render one owner-scoped launchd plist with absolute program paths."""
    service = _NATIVE_CONSTANTS[name]
    resolved_native = native_dir.resolve()
    resolved_home = ava_home.resolve()
    substitutions = {
        "config": str(resolved_native / "config"),
        "data": str(resolved_native / "data"),
    }
    plist: dict[str, Any] = {
        "Label": native_label(name, ava_home),
        "ProgramArguments": [
            str(resolved_native / "bin" / name),
            *[argument.format(**substitutions) for argument in service.arguments],
        ],
        "EnvironmentVariables": {"AVA_HOME": str(resolved_home), "GOMEMLIMIT": service.gomemlimit},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(resolved_home / "lgtm/native/logs" / f"{name}.log"),
        "StandardErrorPath": str(resolved_home / "lgtm/native/logs" / f"{name}.log"),
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _stream_download(url: str, destination: Path) -> None:
    """Download one archive with bounded time and periodic progress reporting."""
    started = time.monotonic()
    last_progress = started
    received = 0
    total: int | None = None
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_SOCKET_TIMEOUT_S) as response,  # noqa: S310 - pinned upstream asset
        destination.open("wb") as output,
    ):
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = None
        while chunk := response.read(1 << 16):
            output.write(chunk)
            received += len(chunk)
            now = time.monotonic()
            if now - last_progress >= _DOWNLOAD_PROGRESS_INTERVAL_S:
                percent = f" ({received * 100 // total}%)" if total else ""
                print(f"  · lgtm native: {received / 1e6:.1f} MB{percent} in {now - started:.0f}s")
                last_progress = now
            if now - started > _DOWNLOAD_ATTEMPT_TIMEOUT_S:
                raise TimeoutError(
                    f"download exceeded {_DOWNLOAD_ATTEMPT_TIMEOUT_S:.0f}s wall-clock cap"
                )
    print(
        f"  · lgtm native: downloaded {received / 1e6:.1f} MB in {time.monotonic() - started:.0f}s"
    )


def _download_with_retry(url: str, archive: Path) -> None:
    """Retry a pinned archive download a bounded number of times."""
    started = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            _stream_download(url, archive)
            return
        except Exception as exc:  # network and filesystem failures are all retriable here
            last_error = exc
            elapsed = time.monotonic() - started
            print(
                f"  ! lgtm native: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} failed "
                f"after {elapsed:.0f}s: {exc}",
                file=sys.stderr,
            )
            if attempt < _DOWNLOAD_ATTEMPTS:
                time.sleep(_DOWNLOAD_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"failed to download native LGTM backend from {url} after {_DOWNLOAD_ATTEMPTS} "
        f"attempts ({time.monotonic() - started:.0f}s total): {last_error}"
    ) from last_error


def _extract_member(name: str, archive: Path, destination: Path) -> None:
    """Copy the expected release member into its final binary path."""
    member = _NATIVE_CONSTANTS[name].archive_member
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = Path(temporary_dir) / name
        if archive.suffix == ".zip":
            with (
                zipfile.ZipFile(archive) as bundle,
                bundle.open(member) as source,
                extracted.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
        else:
            with tarfile.open(archive) as bundle:
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"native LGTM {name} archive has no {member} member")
                with source, extracted.open("wb") as target:
                    shutil.copyfileobj(source, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), destination)
    destination.chmod(0o755)


def _download_and_verify(name: str, version: str, asset: dict[str, str], native_dir: Path) -> None:
    """Download, hash-verify, and install one pinned native backend binary."""
    with tempfile.TemporaryDirectory() as temporary_dir:
        archive = Path(temporary_dir) / Path(asset["url"]).name
        print(f"  · lgtm native: downloading {name} {version} from {asset['url']}")
        _download_with_retry(asset["url"], archive)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != asset["sha256"]:
            raise RuntimeError(
                f"native LGTM {name} {version} SHA256 mismatch: got {actual}, "
                f"expected {asset['sha256']} — refusing to install"
            )
        _extract_member(name, archive, native_dir / "bin" / name)
    (native_dir / f"version-{name}").write_text(version + "\n", encoding="utf-8")


def _write_if_changed(path: Path, content: str) -> None:
    """Publish text only when it differs, avoiding needless launchd churn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _render_configs(repo: Path, native_dir: Path, ava_home: Path) -> None:
    """Render native templates with the sole supported placeholder."""
    source_dir = repo / "deploy/lgtm/native/config"
    for name in ("loki.yaml", "prometheus.yml"):
        template = (source_dir / name).read_text(encoding="utf-8")
        _write_if_changed(
            native_dir / "config" / name, template.replace("{{AVA_HOME}}", str(ava_home))
        )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one local launchctl command without surfacing absent-job failures."""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCHCTL_TIMEOUT_S,
    )


def _job_loaded(label: str) -> bool:
    """Whether launchd currently has a user job for this label."""
    return _launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def _loaded_user_job_labels() -> set[str]:
    """Return labels reported by launchd's bounded user-job listing."""
    result = _launchctl("list")
    if result.returncode != 0:
        return set()
    labels: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3:
            labels.add(fields[2])
    return labels


def _retire_other_native_jobs(ava_home: Path) -> None:
    """Remove legacy and foreign native jobs that race this host's fixed ports."""
    loaded_labels = _loaded_user_job_labels()
    for name in _NATIVE_CONSTANTS:
        current = native_label(name, ava_home)
        competing_loaded = {
            label
            for label in loaded_labels
            if re.fullmatch(rf"com\.ava\.{re.escape(name)}(?:\..+)?", label) and label != current
        }
        plists: dict[str, Path] = {}
        for plist in _agents_dir().glob(f"com.ava.{name}*.plist"):
            try:
                label = plistlib.loads(plist.read_bytes())["Label"]
            except (KeyError, OSError, plistlib.InvalidFileException):
                continue
            if not isinstance(label, str):
                continue
            if label == current:
                continue
            plists[label] = plist
        for label in set(plists) | competing_loaded:
            plist = plists.get(label, _plist_path(label))
            was_loaded = label in competing_loaded or _job_loaded(label)
            booted_out = (
                _launchctl("bootout", f"gui/{os.getuid()}/{label}").returncode == 0
                if was_loaded
                else False
            )
            try:
                plist.unlink()
            except FileNotFoundError:
                removed_plist = False
            else:
                removed_plist = True
            if booted_out or removed_plist:
                logger.info("retired competing native LGTM job {} ({})", label, plist)


def bootout_native_jobs(ava_home: Path) -> None:
    """Boot out and delete this home's jobs; `ava lgtm off` should call it after marker removal."""
    for name in _NATIVE_CONSTANTS:
        label = native_label(name, ava_home)
        if _job_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        _plist_path(label).unlink(missing_ok=True)


def ensure_lgtm_native(repo: Path, ava_home: Path) -> None:
    """Install current backend binaries and always converge configs and plists."""
    tag = platform_tag()
    if tag not in SUPPORTED_TAGS:
        print(
            f"  ! lgtm native: no pinned binaries for {platform.system()} {platform.machine()} — skipped",
            file=sys.stderr,
        )
        return
    native_dir = ava_home / "lgtm/native"
    for directory in ("bin", "config", "data", "logs"):
        (native_dir / directory).mkdir(parents=True, exist_ok=True)
    for name, asset in _load_versions(repo).items():
        marker = native_dir / f"version-{name}"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == asset["version"]:
            print(f"  · lgtm native: {name} {asset['version']} present")
            continue
        _download_and_verify(name, asset["version"], asset, native_dir)
        print(f"  · lgtm native: installed {name} {asset['version']} ({tag})")
    _render_configs(repo, native_dir, ava_home)
    for name in _NATIVE_CONSTANTS:
        label = native_label(name, ava_home)
        _write_if_changed(_plist_path(label), _render_plist(name, native_dir, ava_home))
    if (ava_home / "lgtm-host").exists():
        _retire_other_native_jobs(ava_home)


def ensure_lgtm_native_step(ctx: ConvergeCtx) -> None:
    """Converge the native backends only on the marked LGTM host home."""
    if not (ctx.ava_home / "lgtm-host").exists():
        return
    ensure_lgtm_native(ctx.repo, ctx.ava_home)


def backend_pids(native_dir: Path) -> dict[str, str | None]:
    """Return the running PID for each exact native binary path, if any."""
    pids: dict[str, str | None] = {}
    for name in _NATIVE_CONSTANTS:
        binary = (native_dir / "bin" / name).resolve()
        result = subprocess.run(
            ["pgrep", "-f", rf"^{re.escape(str(binary))}( |$)"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids[name] = (
            result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None
        )
    return pids
