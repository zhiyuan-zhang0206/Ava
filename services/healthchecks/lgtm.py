"""LGTM stack healthcheck — called every 60s by the gateway watchdog.

Runs only on the designated LGTM host: the `$AVA_HOME/lgtm-host` marker file
(operator-created once, machine-identity-file pattern — see
deploy/lgtm/README.md) gates this check the same way it gates the converge
bring-up. Without the marker the check is a no-op, so a dev worktree
cluster's watchdog never touches the host-singleton containers (fixed host
ports, one compose project per host).

Probes the four readiness endpoints on the compose file's fixed host ports:
Loki /ready, Prometheus /-/ready, Tempo /ready, Grafana /api/health. Any
HTTP answer counts as alive (a 503 is a warming-up backend, not a dead one);
only a connection-level failure means the container — or the docker daemon —
is down, and then the fix is re-running the idempotent deploy/lgtm/start.sh
(starts OrbStack on macOS if needed, then `docker compose up -d`). Same
connection-level contract as the otel_collector sidecar check.

The stack is the cluster's observability backend: while it is down the
gateway's /ops + inspect reads (Loki/Prometheus), the Grafana-evaluated ops
alerts, and the events-maintenance Loki rollup all degrade. Telemetry is not
lost meanwhile — the native sidecar buffers in its file-backed queue.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import ava_home

# The four backends' readiness endpoints, on the fixed host ports the compose
# file publishes (the marker pins this check to the host running that compose,
# so loopback + fixed ports IS the contract, not a configurable endpoint).
READINESS_PROBES: tuple[tuple[str, str], ...] = (
    ("loki", "http://127.0.0.1:3100/ready"),
    ("prometheus", "http://127.0.0.1:9090/-/ready"),
    ("tempo", "http://127.0.0.1:3200/ready"),
    ("grafana", "http://127.0.0.1:3003/api/health"),
)


def lgtm_host_marker() -> Path:
    """The machine-identity marker that designates THIS host as the one running
    the LGTM compose stack (`$AVA_HOME/lgtm-host`, operator-created once)."""
    return ava_home() / "lgtm-host"


def is_lgtm_host() -> bool:
    return lgtm_host_marker().exists()


def lgtm_compose_dir(repo: Path) -> Path:
    return repo / "deploy" / "lgtm"


def _endpoint_answers(url: str) -> bool:
    """Any HTTP answer = the container's listener is up; connection failure = down."""
    try:
        with urllib.request.urlopen(url, timeout=2.0):  # noqa: S310 — loopback probe, deliberate
            return True
    except urllib.error.HTTPError:
        return True  # any status proves the listener answered
    except Exception:
        return False


def probe_statuses() -> list[tuple[str, bool]]:
    """(backend name, listener answered) for each of the four readiness probes."""
    return [(name, _endpoint_answers(url)) for name, url in READINESS_PROBES]


def down_probes() -> list[str]:
    """Names of the backends whose readiness endpoint did not answer at all."""
    return [name for name, up in probe_statuses() if not up]


def _restart_stack() -> bool:
    """Re-run the idempotent start.sh (docker daemon bring-up + compose up -d)."""
    repo = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_compose_dir(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        sys.stderr.write(f"lgtm start.sh failed (exit {result.returncode}): {result.stderr}\n")
    return result.returncode == 0


def main() -> None:
    init_gateway_process("lgtm")
    if not is_lgtm_host():
        return  # not the designated LGTM host — nothing to keep alive here
    down = down_probes()
    if not down:
        return
    sys.stderr.write(f"lgtm backends down ({', '.join(down)}) — re-running start.sh\n")
    if not _restart_stack():
        sys.exit(1)


if __name__ == "__main__":
    main()
