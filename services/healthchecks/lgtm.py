"""Hybrid LGTM healthcheck — called every 60s by the gateway watchdog.

Runs only on the designated LGTM host: the `$AVA_HOME/lgtm-host` marker file
(operator-created once, machine-identity-file pattern — see
deploy/lgtm/README.md) gates this check the same way it gates the converge
bring-up. Without the marker the check is a no-op, so a dev worktree
cluster's watchdog never touches the host-singleton backends (fixed host
ports, one native lifecycle per host).

Probes the three LOCAL readiness endpoints: Loki /ready, Prometheus /-/ready,
and Grafana /api/health. Tempo is a remote WSL service, so it is deliberately
outside this repair loop: its failure must not restart local backends. Any HTTP
answer counts as alive (a 503 is a warming-up backend, not a dead one); only a
connection-level failure means a local backend is down, and then the fix is
re-running the idempotent deploy/lgtm/start.sh. Same connection-level contract
as the otel_collector sidecar check.

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

# The locally managed backends' readiness endpoints, on their fixed host ports.
# The marker pins this check to the owner host, so loopback + fixed ports is the
# contract. Remote Tempo intentionally has no readiness probe here.
READINESS_PROBES: tuple[tuple[str, str], ...] = (
    ("loki", "http://127.0.0.1:3100/ready"),
    ("prometheus", "http://127.0.0.1:9090/-/ready"),
    ("grafana", "http://127.0.0.1:3003/api/health"),
)


def lgtm_host_marker() -> Path:
    """The machine-identity marker that designates THIS host as the one running
    the local LGTM backends (`$AVA_HOME/lgtm-host`, operator-created once)."""
    return ava_home() / "lgtm-host"


def is_lgtm_host() -> bool:
    return lgtm_host_marker().exists()


def lgtm_deploy_dir(repo: Path) -> Path:
    return repo / "deploy" / "lgtm"


def _endpoint_answers(url: str) -> bool:
    """Any HTTP answer = the backend listener is up; connection failure = down."""
    try:
        with urllib.request.urlopen(url, timeout=2.0):  # noqa: S310 — loopback probe, deliberate
            return True
    except urllib.error.HTTPError:
        return True  # any status proves the listener answered
    except Exception:
        return False


def probe_statuses() -> list[tuple[str, bool]]:
    """(backend name, listener answered) for each local readiness probe."""
    return [(name, _endpoint_answers(url)) for name, url in READINESS_PROBES]


def down_probes() -> list[str]:
    """Names of the backends whose readiness endpoint did not answer at all."""
    return [name for name, up in probe_statuses() if not up]


def _restart_stack() -> bool:
    """Re-run start.sh; its probe-first path never restarts a live backend."""
    repo = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_deploy_dir(repo),
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
