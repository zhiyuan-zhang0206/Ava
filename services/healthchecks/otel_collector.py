"""OTel Collector sidecar healthcheck — called every 60s by the watchdog.

Probes the sidecar's OTLP/HTTP receiver at 127.0.0.1:4318: POST an OTLP/JSON
body to `/v1/traces` — ANY HTTP status (200, 400, 415...) proves the
collector's OTLP listener answered the port. Only a connection-level failure
means the sidecar is down. This is the same probe the agent exporters use at
init (`shared.telemetry_otlp.endpoint_reachable`), so the healthcheck and the
agents agree on what "the sidecar is up" means. On death, respawn through the
service backend with the same command the roster carries.

The sidecar is the local OTLP entry for every agent on this machine — when it
is down, trace/event/metric export drops (agents retry briefly, then shed)
until the watchdog revives it within a minute.
"""

import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import otel_collector_binary, otel_collector_config
from shared.service_respawn import respawn_service

_PORT = 4318


def _is_alive() -> bool:
    """Any HTTP answer from the OTLP /v1/traces path = alive."""
    try:
        req = urllib.request.Request(  # noqa: S310 — loopback probe, deliberate
            urllib.parse.urljoin("http://127.0.0.1:4318/", "v1/traces"),
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0):  # noqa: S310 — same probe
            return True
    except urllib.error.HTTPError:
        return True  # any status proves the listener answered
    except Exception:
        return False


def _restart_daemon() -> bool:
    """Start the collector in the ava-otel-collector session via the service backend."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_service(
        "otel-collector",
        f"{otel_collector_binary()} --config {otel_collector_config()}",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
    )


def main() -> None:
    init_gateway_process("otel-collector")
    if _is_alive():
        return
    sys.stderr.write("ava-otel-collector down — restarting\n")
    if not _restart_daemon():
        sys.exit(1)


if __name__ == "__main__":
    main()
