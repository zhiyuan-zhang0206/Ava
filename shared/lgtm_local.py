"""Local native LGTM listener identity, independent of external query URLs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.config import settings


@dataclass(frozen=True)
class _NativeService:
    arguments: tuple[str, ...]
    gomemlimit: str | None
    archive_member: str | None
    binary_path: str
    uses_run_script: bool = False


NATIVE_SERVICES: dict[str, _NativeService] = {
    "loki": _NativeService(
        arguments=("-config.file={config}/loki.yaml",),
        gomemlimit="2GiB",
        archive_member="loki-darwin-arm64",
        binary_path="bin/loki",
    ),
    "prometheus": _NativeService(
        arguments=(
            "--config.file={config}/prometheus.yml",
            "--storage.tsdb.path={data}/prom",
            "--storage.tsdb.retention.time=180h",
            "--storage.tsdb.retention.size=8GB",
            "--web.enable-otlp-receiver",
            "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}",
        ),
        gomemlimit="1GiB",
        archive_member="prometheus-3.13.2.darwin-arm64/prometheus",
        binary_path="bin/prometheus",
    ),
    "grafana": _NativeService(
        arguments=(
            "server",
            "--config={config}/grafana.ini",
            "--homepath={homepath}",
        ),
        gomemlimit=None,
        archive_member=None,
        binary_path="grafana-home/bin/grafana",
        uses_run_script=True,
    ),
}


BACKENDS = tuple(NATIVE_SERVICES)

# Each backend's own supported health/readiness path. Local lifecycle probes
# must use these, NOT the root URL: Grafana's root redirects to the public
# Gateway root (serve_from_sub_path), and following that redirect while the
# Gateway is still waiting on the same start() makes a healthy Grafana read
# as down and gets it restarted (#2047).
HEALTH_PATHS = {
    "loki": "/ready",
    "prometheus": "/-/ready",
    "grafana": "/api/health",
}


def binary_path(home: Path, name: str) -> Path:
    """The exact installed executable owned by this home."""
    relative = {
        "loki": "bin/loki",
        "prometheus": "bin/prometheus",
        "grafana": "grafana-home/bin/grafana",
    }[name]
    return (home / "lgtm/native" / relative).resolve()


def backend_urls() -> dict[str, str]:
    """Probe the configured local binds; wildcard listeners are dialed on loopback."""
    obs = settings.observability
    ports = {
        "loki": obs.lgtm_loki_port,
        "prometheus": obs.lgtm_prometheus_port,
        "grafana": obs.lgtm_grafana_port,
    }
    urls: dict[str, str] = {}
    for name, port in ports.items():
        host = obs.lgtm_grafana_listen_host if name == "grafana" else obs.lgtm_listen_host
        host = host.removeprefix("[").removesuffix("]")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104 — wildcard classification, never a bind
        urls[name] = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    return urls


def storage_dir(home: Path) -> Path:
    """The explicit local observation data volume, retained across service stops."""
    configured = settings.observability.lgtm_storage_dir.strip()
    return Path(configured).expanduser().resolve() if configured else home / "lgtm/native/data"


def service_argv(home: Path, name: str) -> tuple[str, ...]:
    """One native invocation, launched only as an ava-root unit."""
    native = home.resolve() / "lgtm/native"
    spec = NATIVE_SERVICES[name]
    if spec.uses_run_script:
        return (str(native / "grafana/run.sh"),)
    substitutions = {
        "config": str(native / "config"),
        "data": str(storage_dir(home)),
        "homepath": str(native / "grafana-home"),
        "lgtm_listen_host": settings.observability.lgtm_listen_host,
        "lgtm_prometheus_port": str(settings.observability.lgtm_prometheus_port),
    }
    return (str(binary_path(home, name)), *(s.format(**substitutions) for s in spec.arguments))


def service_input_paths(home: Path, name: str) -> tuple[Path, ...]:
    """Declare the complete configuration tree; durable data is not a birth input."""
    native = home / "lgtm/native"
    paths = [native / f"version-{name}", native / f"platform-{name}", native / "config"]
    if name == "grafana":
        paths.extend([native / "grafana/run.sh", native / "grafana/admin_password"])
    return tuple(paths)


def service_environment(name: str) -> dict[str, str]:
    """Process environment independent of the manifest's typed input seals."""
    limit = NATIVE_SERVICES[name].gomemlimit
    return {} if limit is None else {"GOMEMLIMIT": limit}
