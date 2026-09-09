"""Local native LGTM listener identity, independent of external query URLs."""

from __future__ import annotations

from pathlib import Path

from shared.config import settings
from shared.process_env import inherited_process_env

BACKENDS = ("loki", "prometheus", "grafana")

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


def lifecycle_environment() -> dict[str, str]:
    """Pass resolved native listeners to the Darwin source script."""
    return inherited_process_env(
        {f"AVA_NATIVE_{name.upper()}_URL": url for name, url in backend_urls().items()}
    )
