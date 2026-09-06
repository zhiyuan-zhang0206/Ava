"""Native LGTM installation and host service configuration.

Converge downloads only a missing or stale release asset, while always
rendering live configs and launchd plists / user systemd units so changes apply on the
next lifecycle run.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
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
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._lgtm import is_station_ctx, roles_declare_station
from cli.commands._lgtm_assets import _NATIVE_CONSTANTS, load_versions
from cli.commands._observatory_urls import (
    _alerts_webhook_url,
    _atomic_write,
    _observability_datasource_urls,
)
from cli.commands._rendered_file import write_rendered_guarded
from shared.log import logger
from shared.loki_index_labels import validate_loki_deploy_config

SUPPORTED_TAGS = {"darwin_arm64", "linux_amd64"}

_DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
_DOWNLOAD_ATTEMPT_TIMEOUT_S = 600.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5.0
_DOWNLOAD_PROGRESS_INTERVAL_S = 15.0
_LAUNCHCTL_TIMEOUT_S = 5.0


def platform_tag() -> str | None:
    """Return the pinned release tag for this machine, if native LGTM supports it."""
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "darwin_arm64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux_amd64"
    return None


def _load_versions(repo: Path) -> dict[str, dict[str, str]]:
    tag = platform_tag()
    if tag is None:
        raise RuntimeError("Native LGTM has no pinned assets for this platform")
    return load_versions(repo, tag)


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


def _binary_path(name: str, native_dir: Path) -> Path:
    """Return the installed executable path for one native backend."""
    return native_dir / _NATIVE_CONSTANTS[name].binary_path


def _storage_dir(ava_home: Path) -> Path:
    """The observation-data root for the native backends, per machine.

    `AVA_LGTM_STORAGE_DIR` (empty = default) selects where Loki's filesystem
    store and Prometheus' TSDB live; the default resolves to
    `$AVA_HOME/lgtm/native/data`, byte-identical to the historical layout. The
    knob is host-scope — it names a machine's data volume, not a cluster's.
    """
    from shared.config import settings

    configured = settings.observability.lgtm_storage_dir.strip()
    if not configured:
        return (ava_home / "lgtm" / "native" / "data").resolve()
    return Path(configured).expanduser().resolve()


def _service_invocation(
    name: str, native_dir: Path, ava_home: Path
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The same native command and environment for launchd and user systemd."""
    from shared.config import settings

    service = _NATIVE_CONSTANTS[name]
    resolved_native = native_dir.resolve()
    resolved_home = ava_home.resolve()
    substitutions = {
        "config": str(resolved_native / "config"),
        "data": str(_storage_dir(ava_home)),
        "homepath": str(resolved_native / "grafana-home"),
        "lgtm_listen_host": settings.observability.lgtm_listen_host,
        "lgtm_prometheus_port": str(settings.observability.lgtm_prometheus_port),
    }
    program_arguments = (
        [str(resolved_native / "grafana" / "run.sh")]
        if service.uses_run_script
        else [
            str(_binary_path(name, resolved_native)),
            *[argument.format(**substitutions) for argument in service.arguments],
        ]
    )
    environment = {"AVA_HOME": str(resolved_home)}
    if service.gomemlimit is not None:
        environment["GOMEMLIMIT"] = service.gomemlimit
    return tuple(program_arguments), environment


def _render_plist(name: str, native_dir: Path, ava_home: Path) -> str:
    """Render one owner-scoped launchd plist with absolute program paths."""
    program_arguments, environment = _service_invocation(name, native_dir, ava_home)
    resolved_home = ava_home.resolve()
    plist: dict[str, Any] = {
        "Label": native_label(name, ava_home),
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": environment,
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


def _extract_member(name: str, archive: Path, destination: Path, member: str | None = None) -> None:
    """Copy the expected release member into its final binary path."""
    member = member or _NATIVE_CONSTANTS[name].archive_member
    if member is None:
        raise RuntimeError(f"native LGTM {name} must install a release tree")
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


def _extract_tree(name: str, archive: Path, destination: Path) -> None:
    """Install a release archive tree after stripping its top-level directory."""
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = Path(temporary_dir) / "extracted"
        extracted.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted, filter="data")
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(f"native LGTM {name} archive must have one top-level directory")
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(roots[0]), destination)


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
        service = _NATIVE_CONSTANTS[name]
        if service.archive_member is None:
            _extract_tree(name, archive, native_dir / "grafana-home")
        else:
            _extract_member(name, archive, _binary_path(name, native_dir), asset["member"])
    (native_dir / f"version-{name}").write_text(version + "\n", encoding="utf-8")
    (native_dir / f"platform-{name}").write_text(str(platform_tag()) + "\n", encoding="utf-8")


def _write_if_changed(path: Path, content: str, *, mode: int | None = None) -> None:
    """Publish text only when it differs, avoiding needless launchd churn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        if mode is not None:
            path.chmod(mode)
        return
    if mode is None:
        path.write_text(content, encoding="utf-8")
        return
    if path.exists():
        path.chmod(mode)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        output.write(content)


def _has_loopback_host(url: str) -> bool:
    """Whether a configured URL resolves to a loopback hostname or address."""
    hostname = urlparse(url).hostname
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _url_host_port(url: str) -> str:
    """`host:port` of a base URL — scheme and trailing slash stripped, the
    Prometheus scrape-target form (the tempo target substitution's shape)."""
    return url.rstrip("/").removeprefix("http://").removeprefix("https://")


def _is_specific_non_loopback_listen(host: str) -> bool:
    """Whether a listen host binds one specific address that loopback dials miss.

    A wildcard bind (0.0.0.0 / ::) or a loopback bind still answers on
    loopback, so neither breaks the default loopback read URLs. Only a specific
    non-loopback IP does. Hostnames are not judged (resolution is out of scope
    here); the external-migration form uses literal tailnet IPs.
    """
    h = host.strip().lower().removeprefix("[").removesuffix("]")
    if h in ("0.0.0.0", "::", "localhost", "::1") or h.startswith("127."):  # noqa: S104 — wildcard classification, not a bind
        return False
    try:
        return not ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _warn_listen_read_mismatches() -> None:
    """Warn when a native backend binds a specific non-loopback address while
    its telemetry read URL still resolves to loopback.

    Prometheus scrape targets and consumer reads use the telemetry URLs, so
    a widened listen host with loopback read URLs breaks those read paths (QA nit on PR #727, Task #1795)."""
    from shared.config import settings

    observability = settings.observability
    backends = (
        (
            "loki",
            "AVA_TELEMETRY_LOKI_URL",
            observability.lgtm_listen_host,
            observability.telemetry_loki_url,
        ),
        (
            "prometheus",
            "AVA_TELEMETRY_PROMETHEUS_URL",
            observability.lgtm_listen_host,
            observability.telemetry_prometheus_url,
        ),
        (
            "grafana",
            "AVA_TELEMETRY_GRAFANA_URL",
            observability.lgtm_grafana_listen_host,
            observability.telemetry_grafana_url,
        ),
    )
    for backend, env_var, listen_host, read_url in backends:
        if not _is_specific_non_loopback_listen(listen_host) or not _has_loopback_host(read_url):
            continue
        print(
            "lgtm native: "
            f"{env_var} resolves to {read_url} but {backend} listens on {listen_host} "
            f"— set {env_var} to the listen address or keep the listen host loopback",
            file=sys.stderr,
        )


def _render_configs(repo: Path, native_dir: Path, ava_home: Path) -> None:
    """Render native templates from this checkout and host configuration."""
    from shared.config import settings

    source_dir = repo / "deploy/lgtm/native/config"
    # Grafana provisioning is converge-rendered (datasources.yml / contact.yml
    # carry settings-baked URLs); grafana reads the rendered tree, never the
    # source checkout directly.
    rendered_provisioning = native_dir / "config" / "provisioning"
    tempo_query_url = settings.observability.telemetry_tempo_query_url.rstrip("/")
    tempo_intake_endpoint = settings.observability.telemetry_tempo_endpoint.rstrip("/")
    lgtm_listen_host = settings.observability.lgtm_listen_host
    if _has_loopback_host(tempo_query_url) != _has_loopback_host(tempo_intake_endpoint):
        print(
            "lgtm native: AVA_TELEMETRY_TEMPO_QUERY_URL resolves to "
            f"{tempo_query_url} but the Tempo intake endpoint is {tempo_intake_endpoint} "
            "— set AVA_TELEMETRY_TEMPO_QUERY_URL to the remote cluster's query URL",
            file=sys.stderr,
        )
    _warn_listen_read_mismatches()
    loki_url, prometheus_url, pg_url = _observability_datasource_urls()
    substitutions = {
        "AVA_HOME": str(ava_home),
        "LGTM_STORAGE_DIR": str(_storage_dir(ava_home)),
        "AVA_PROVISIONING_PATH": str(rendered_provisioning),
        "GRAFANA_PROVISIONING_PATH": str(rendered_provisioning / "dashboards"),
        "AVA_TEMPO_QUERY_URL": tempo_query_url,
        # Two-state datasource + webhook URLs: empty AVA_OBSERVABILITY_URL keeps
        # the loopback defaults (byte-identical to pre-parameterization); the
        # provisioning files consume them via Grafana's $__env{} expansion.
        "LOKI_URL": loki_url,
        "PROMETHEUS_URL": prometheus_url,
        "PG_URL": pg_url,
        "ALERTS_WEBHOOK_URL": _alerts_webhook_url(),
        "REPO": str(repo),
        "lgtm_grafana_listen_host": settings.observability.lgtm_grafana_listen_host,
        "LGTM_LOKI_PORT": str(settings.observability.lgtm_loki_port),
        "LGTM_GRAFANA_PORT": str(settings.observability.lgtm_grafana_port),
    }
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        template = (source_dir / name).read_text(encoding="utf-8")
        template_substitutions = substitutions
        if name == "prometheus.yml":
            template_substitutions = {
                **substitutions,
                "AVA_TEMPO_QUERY_URL": _url_host_port(tempo_query_url),
                "AVA_PROMETHEUS_SCRAPE_TARGET": _url_host_port(
                    settings.observability.telemetry_prometheus_url
                ),
                "AVA_LOKI_SCRAPE_TARGET": _url_host_port(settings.observability.telemetry_loki_url),
                "AVA_GRAFANA_SCRAPE_TARGET": _url_host_port(
                    settings.observability.telemetry_grafana_url
                ),
            }
        content = template
        for key, value in template_substitutions.items():
            content = content.replace(f"{{{{{key}}}}}", value)
        # The loki listener host cannot ride the {{...}} pass: a YAML plain
        # scalar must not start with '{' (it would parse as a flow mapping), and
        # the rendered line must stay byte-identical to the historical unquoted
        # address — so the template carries a brace-free token instead.
        content = content.replace("__LGTM_LISTEN_HOST__", lgtm_listen_host)
        content = content.replace("__LGTM_LOKI_PORT__", str(settings.observability.lgtm_loki_port))
        content = content.replace(
            "__LGTM_LOKI_GRPC_PORT__", str(settings.observability.lgtm_loki_grpc_port)
        )
        if name == "loki.yaml":
            rendered = yaml.safe_load(content)
            if not isinstance(rendered, dict):
                raise TypeError("native Loki config must render to a mapping")
            validate_loki_deploy_config(cast(dict[str, object], rendered))
        _write_if_changed(native_dir / "config" / name, content)
    run_script = (source_dir / "run.sh").read_text(encoding="utf-8")
    for key, value in substitutions.items():
        run_script = run_script.replace(f"{{{{{key}}}}}", value)
    rendered_run_script = native_dir / "grafana" / "run.sh"
    _write_if_changed(rendered_run_script, run_script)
    rendered_run_script.chmod(0o755)
    _render_provisioning(repo, native_dir)


def _render_provisioning(repo: Path, native_dir: Path) -> None:
    """Copy the Grafana provisioning tree into the native config dir.

    Every provisioning file (datasources, alert rules, dashboards, READMEs) is
    copied VERBATIM — the datasource and webhook URLs are Grafana-native
    $__env{} references resolved from the process env (runtime.env), so the
    checkout file is always a valid configuration and the rendered copy never
    rewrites URLs. Each file is written atomically through the content-hash
    user-modification guard (web-sources precedent): a file the user
    hand-edited since the last converge write is warned about and preserved,
    never overwritten. Grafana reads this rendered tree via
    {{AVA_PROVISIONING_PATH}} — the deployment state is decoupled from the
    source checkout, so a rollout that swaps the checkout cannot feed the
    running instance a half-rendered tree.
    """
    source_dir = repo / "deploy/lgtm/config/grafana/provisioning"
    if not source_dir.is_dir():
        return
    dest_dir = native_dir / "config" / "provisioning"
    hashes_path = native_dir / "config" / "provisioning-hashes.json"
    rendered_relative: set[str] = set()
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(source_dir)
        rendered_relative.add(rel.as_posix())
        content = source.read_text(encoding="utf-8")
        warning = write_rendered_guarded(
            dest_dir / rel, content, hashes_path, rel.as_posix(), writer=_atomic_write
        )
        if warning is not None:
            print(f"  ! lgtm native: {warning}", file=sys.stderr)
    # Remove rendered files whose source template vanished (web-sources
    # _cleanup_gone_sources): untouched copies are pure derived state; a copy
    # the user edited is kept, loudly.
    for dest in sorted(dest_dir.rglob("*")):
        if not dest.is_file():
            continue
        rel = dest.relative_to(dest_dir).as_posix()
        if rel in rendered_relative:
            continue
        hashes: dict[str, str] = {}
        if hashes_path.exists():
            hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
        recorded = hashes.get(rel)
        if recorded is not None and hashlib.sha256(dest.read_bytes()).hexdigest() == recorded:
            dest.unlink()
            continue
        print(
            f"  ! lgtm native: rendered provisioning file {dest} has no source "
            "template anymore but was modified locally; kept",
            file=sys.stderr,
        )


def _render_grafana_admin_password(native_dir: Path) -> None:
    """Render the host-scoped Grafana credential when the setting is configured."""
    from shared.config import settings

    credential = settings.alerts.grafana_admin_password
    if credential is None:
        return
    credential_file = native_dir / "grafana/admin_password"
    _write_if_changed(credential_file, credential.get_secret_value() + "\n", mode=0o600)


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
        competitors = set(plists) | competing_loaded
        if not competitors or not _job_loaded(current):
            continue
        for label in competitors:
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
    if platform.system() == "Linux":
        from shared.lgtm_systemd import stop

        stop(ava_home)
        return
    for name in _NATIVE_CONSTANTS:
        label = native_label(name, ava_home)
        if _job_loaded(label) and _launchctl("bootout", f"gui/{os.getuid()}/{label}").returncode:
            raise RuntimeError(f"Failed to stop native LGTM job {label}")
        _plist_path(label).unlink(missing_ok=True)


def ensure_lgtm_native(repo: Path, ava_home: Path, *, station: bool = False) -> None:
    """Install current binaries and converge configs and native service definitions.

    `station` is the declarative provider identity (the `observability-station`
    capability); the legacy `$AVA_HOME/lgtm-host` marker is still honored on its
    own, so the existing marked host behaves exactly as before.
    """
    tag = platform_tag()
    if tag not in SUPPORTED_TAGS:
        print(
            f"  ! lgtm native: no pinned binaries for {platform.system()} {platform.machine()} — skipped",
            file=sys.stderr,
        )
        return
    native_dir = ava_home / "lgtm/native"
    for directory in ("bin", "config", "logs"):
        (native_dir / directory).mkdir(parents=True, exist_ok=True)
    storage = _storage_dir(ava_home)
    for sub in ("", "loki", "prom"):
        (storage / sub).mkdir(parents=True, exist_ok=True)
    config_before = _linux_config_fingerprint(native_dir)
    for name, asset in _load_versions(repo).items():
        marker = native_dir / f"version-{name}"
        platform_marker = native_dir / f"platform-{name}"
        matching_platform = (
            platform_marker.read_text().strip() == tag
            if platform_marker.exists()
            else tag == "darwin_arm64"
        )
        if matching_platform and marker.exists() and marker.read_text().strip() == asset["version"]:
            print(f"  · lgtm native: {name} {asset['version']} present")
            continue
        _download_and_verify(name, asset["version"], asset, native_dir)
        print(f"  · lgtm native: installed {name} {asset['version']} ({tag})")
    grafana_config_before = _grafana_config_fingerprint(native_dir)
    _render_configs(repo, native_dir, ava_home)
    _render_grafana_admin_password(native_dir)
    if tag == "linux_amd64":
        from shared import lgtm_systemd

        commands = {
            name: lgtm_systemd.Command(*_service_invocation(name, native_dir, ava_home))
            for name in _NATIVE_CONSTANTS
        }
        units_changed = lgtm_systemd.register(ava_home, commands)
        if units_changed or config_before != _linux_config_fingerprint(native_dir):
            lgtm_systemd.restart_running(ava_home)
        return
    for name in _NATIVE_CONSTANTS:
        label = native_label(name, ava_home)
        _write_if_changed(_plist_path(label), _render_plist(name, native_dir, ava_home))
    if station or (ava_home / "lgtm-host").exists():
        _retire_other_native_jobs(ava_home)
    _restart_grafana_if_config_changed(native_dir, ava_home, grafana_config_before)


def _linux_config_fingerprint(native_dir: Path) -> tuple[str, ...]:
    """Inputs whose changes require restarting an already running Linux backend."""
    paths = [native_dir / "config" / name for name in ("loki.yaml", "prometheus.yml")]
    paths += [native_dir / f"version-{name}" for name in _NATIVE_CONSTANTS]
    paths.append(native_dir / "grafana/run.sh")
    return (
        *_grafana_config_fingerprint(native_dir),
        *(p.read_text() if p.exists() else "" for p in paths),
    )


def _grafana_config_fingerprint(native_dir: Path) -> tuple[str, str, str]:
    """The converge-rendered inputs the RUNNING Grafana serves: the INI (its
    provisioning path lives there), the runtime env (the datasource/webhook
    $__env{} values), and the rendered provisioning tree's hash sidecar (the
    tree content). Comparing the pre/post tuple detects every config change
    that needs a Grafana restart to take effect."""
    config_dir = native_dir / "config"
    ini = config_dir / "grafana.ini"
    runtime_env = config_dir / "runtime.env"
    hashes = config_dir / "provisioning-hashes.json"
    return (
        ini.read_text(encoding="utf-8") if ini.exists() else "",
        runtime_env.read_text(encoding="utf-8") if runtime_env.exists() else "",
        hashes.read_text(encoding="utf-8") if hashes.exists() else "",
    )


def _restart_grafana_if_config_changed(
    native_dir: Path, ava_home: Path, before: tuple[str, str, str]
) -> None:
    """Kickstart Grafana when its converge-rendered config changed.

    A running Grafana never re-reads its INI: after a render that changed the
    provisioning path or the env-baked datasource/webhook URLs, the instance
    keeps serving the OLD config until restarted (the watchdog only acts on
    readiness failure, which broken datasources do not cause). A controlled
    `launchctl kickstart -k` closes that window — same convention the README
    documents for manual config restarts. Only on Darwin with the job loaded;
    the watchdog's 60s readiness window is well above Grafana's ~10s cold
    start. Converge does not wait for the restart.
    """
    if platform.system() != "Darwin":
        return
    after = _grafana_config_fingerprint(native_dir)
    if before == after:
        return
    label = native_label("grafana", ava_home)
    if not _job_loaded(label):
        return
    _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{label}")
    print(
        f"  · lgtm native: grafana config changed; kickstarted {label} "
        "(watchdog readiness window covers the cold start)",
        file=sys.stderr,
    )


def ensure_lgtm_native_step(ctx: ConvergeCtx) -> None:
    """Converge the native backends on the observability station.

    Provider identity is the legacy `lgtm-host` marker OR the declarative
    `observability-station` capability; both render the full native set
    (configs + native service definitions + storage dirs) and install pinned binaries.
    """
    if not is_station_ctx(ctx):
        return
    ensure_lgtm_native(ctx.repo, ctx.ava_home, station=roles_declare_station(ctx.roles))


def backend_pids(native_dir: Path) -> dict[str, str | None]:
    """Return the running PID for each exact native binary path, if any."""
    if platform.system() == "Linux":
        from shared.lgtm_systemd import running_pid

        home = native_dir.parent.parent
        return {
            name: str(pid) if (pid := running_pid(home, name)) else None
            for name in _NATIVE_CONSTANTS
        }
    pids: dict[str, str | None] = {}
    for name in _NATIVE_CONSTANTS:
        binary = _binary_path(name, native_dir).resolve()
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
