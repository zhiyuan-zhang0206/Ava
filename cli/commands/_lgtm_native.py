"""Prepare native LGTM assets and configuration for the root service roster.

Preparation never registers OS jobs, launches services or retires another home.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._lgtm import is_station_ctx
from cli.commands._lgtm_assets import _NATIVE_CONSTANTS, load_versions
from cli.commands._lgtm_provisioning import _render_provisioning
from cli.commands._observatory_urls import (
    _alerts_webhook_url,
    _observability_datasource_urls,
)
from shared.lgtm_local import BACKENDS
from shared.lgtm_local import storage_dir as _storage_dir
from shared.loki_index_labels import validate_loki_deploy_config
from shared.resilience import Policy, retry

SUPPORTED_TAGS = {"darwin_arm64", "linux_amd64"}

_DOWNLOAD_SOCKET_TIMEOUT_S = 30.0
_DOWNLOAD_ATTEMPT_TIMEOUT_S = 600.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5.0
_DOWNLOAD_PROGRESS_INTERVAL_S = 15.0


def _download_backoff(attempt: int) -> float:
    return _DOWNLOAD_RETRY_BACKOFF_S * (attempt + 1)


# Pinned archive download retries every failure on the original 5s, 10s schedule.
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


def _binary_path(name: str, native_dir: Path) -> Path:
    """Return the installed executable path for one native backend."""
    return native_dir / _NATIVE_CONSTANTS[name].binary_path


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
    attempt = 0

    def _download_once() -> None:
        nonlocal attempt
        attempt += 1
        try:
            _stream_download(url, archive)
        except Exception as exc:  # network and filesystem failures are all retriable here
            elapsed = time.monotonic() - started
            print(
                f"  ! lgtm native: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} failed "
                f"after {elapsed:.0f}s: {exc}",
                file=sys.stderr,
            )
            raise

    try:
        retry(_DOWNLOAD_POLICY)(_download_once)
    except Exception as exc:
        raise RuntimeError(
            f"failed to download native LGTM backend from {url} after {_DOWNLOAD_ATTEMPTS} "
            f"attempts ({time.monotonic() - started:.0f}s total): {exc}"
        ) from exc


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


def _warn_env_file_divergence(ava_home: Path, values: dict[str, str]) -> None:
    """Warn when a value about to be rendered disagrees with this unit's .env.

    Settings give an inherited environment value precedence over the file for
    host-scope keys, and a long-lived parent forwards its snapshot to every
    child it spawns — so a converge run inside such a session bakes the stale
    value into the rendered tree silently (2026-09-14 wave: the tempo target
    was re-rendered with the pre-change tailnet URL while .env said loopback;
    task #3339). Comparing against the file makes the divergence loud, whatever
    key it hits. A key the file does not declare is skipped: env-only supply is
    legitimate (a not-yet-enrolled unit, the test suites).
    """
    env_file = ava_home / ".env"
    if not env_file.exists():
        return
    declared = dotenv_values(env_file)
    for alias, value in values.items():
        file_value = declared.get(alias)
        if file_value is None or file_value.rstrip("/") == value.rstrip("/"):
            continue
        print(
            f"lgtm native: {alias} resolved to {value} but {env_file} declares "
            f"{file_value} — an inherited environment value is in effect",
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
    _warn_env_file_divergence(
        ava_home,
        {
            "AVA_TELEMETRY_TEMPO_QUERY_URL": tempo_query_url,
            "AVA_TELEMETRY_TEMPO_ENDPOINT": tempo_intake_endpoint,
            "AVA_TELEMETRY_LOKI_URL": settings.observability.telemetry_loki_url,
            "AVA_TELEMETRY_PROMETHEUS_URL": settings.observability.telemetry_prometheus_url,
            "AVA_TELEMETRY_GRAFANA_URL": settings.observability.telemetry_grafana_url,
            "AVA_OBSERVABILITY_URL": settings.observability.observability_url,
            "AVA_LGTM_LISTEN_HOST": lgtm_listen_host,
            "AVA_LGTM_GRAFANA_LISTEN_HOST": settings.observability.lgtm_grafana_listen_host,
            "AVA_LGTM_LOKI_PORT": str(settings.observability.lgtm_loki_port),
            "AVA_LGTM_GRAFANA_PORT": str(settings.observability.lgtm_grafana_port),
        },
    )
    loki_url, prometheus_url, pg_url = _observability_datasource_urls()
    substitutions = {
        "AVA_HOME": str(ava_home),
        "LGTM_STORAGE_DIR": str(_storage_dir(ava_home)),
        "AVA_PROVISIONING_PATH": str(rendered_provisioning),
        "GRAFANA_PROVISIONING_PATH": str(rendered_provisioning / "dashboards"),
        "AVA_TEMPO_QUERY_URL": tempo_query_url,
        # Datasources and webhooks use their configured service endpoints;
        # provisioning consumes them via Grafana's $__env{} expansion.
        "LOKI_URL": loki_url,
        "PROMETHEUS_URL": prometheus_url,
        "PG_URL": pg_url,
        # run.sh sources runtime.env; URL paths must remain literal shell data.
        "ALERTS_WEBHOOK_URL": shlex.quote(_alerts_webhook_url()),
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


def _render_grafana_admin_password(native_dir: Path) -> None:
    """Render the host-scoped Grafana credential when the setting is configured."""
    from shared.config import settings

    credential = settings.alerts.grafana_admin_password
    if credential is None:
        return
    credential_file = native_dir / "grafana/admin_password"
    _write_if_changed(credential_file, credential.get_secret_value() + "\n", mode=0o600)


def ensure_lgtm_native(repo: Path, ava_home: Path, *, services: frozenset[str]) -> None:
    """Prepare selected pinned binaries and config; lifecycle belongs to ava-root."""
    if not services or services - set(BACKENDS):
        raise ValueError("native LGTM preparation requires selected backend names")
    tag = platform_tag()
    if tag not in SUPPORTED_TAGS:
        raise RuntimeError("Native LGTM has no pinned binaries for this platform")
    native_dir = ava_home / "lgtm/native"
    for directory in ("bin", "config", "logs"):
        (native_dir / directory).mkdir(parents=True, exist_ok=True)
    storage = _storage_dir(ava_home)
    for sub in ("", "loki", "prom"):
        (storage / sub).mkdir(parents=True, exist_ok=True)
    for name, asset in _load_versions(repo).items():
        if name not in services:
            continue
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
    _render_configs(repo, native_dir, ava_home)
    if "grafana" in services:
        _render_grafana_admin_password(native_dir)
    if "loki" in services:
        _verify_loki(ava_home)


def _verify_loki(home: Path) -> None:
    """Use the pinned binary's own parser before starting the root generation."""
    from shared.lgtm_local import binary_path

    result = subprocess.run(
        [
            str(binary_path(home, "loki")),
            f"-config.file={home.resolve()}/lgtm/native/config/loki.yaml",
            "-verify-config",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Loki config verification failed: {result.stderr.strip()}")


def ensure_lgtm_native_step(ctx: ConvergeCtx) -> None:
    """Converge the native backends on the observability station.

    Provider identity is the legacy `lgtm-host` marker OR the declarative
    `observability-station` capability; both render the full native set
    (configs + native service definitions + storage dirs) and install pinned binaries.
    """
    selected = ctx.services.intersection(BACKENDS)
    if not is_station_ctx(ctx) or not selected:
        return
    ensure_lgtm_native(ctx.repo, ctx.ava_home, services=selected)
