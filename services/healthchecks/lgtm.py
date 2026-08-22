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

import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import unset_key

from shared.config import settings
from shared.log import init_gateway_process
from shared.machine import machine_role
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
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OBSOLETE_ROOT_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?GRAFANA_ROOT_URL\s*=", re.MULTILINE)


def lgtm_host_marker() -> Path:
    """The machine-identity marker that designates THIS host as the one running
    the LGTM compose stack (`$AVA_HOME/lgtm-host`, operator-created once)."""
    return ava_home() / "lgtm-host"


def is_lgtm_host() -> bool:
    return lgtm_host_marker().exists()


def lgtm_compose_dir(repo: Path) -> Path:
    return repo / "deploy" / "lgtm"


def grafana_root_url() -> str:
    """Derive Grafana's one browser URL from the gateway's public base.

    Grafana is never advertised on its loopback host port. Reject credentials,
    path prefixes, query strings, and fragments so this derivation cannot turn
    into an open redirect or double-subpath configuration.
    """
    raw = settings.gateway.gateway_url.strip()
    parsed = urlsplit(raw)
    try:
        _port = parsed.port
        port_valid = True
    except ValueError:
        port_valid = False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not port_valid
    ):
        raise RuntimeError(
            "AVA_GATEWAY_URL must be an http(s) origin before Grafana can be started"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "/grafana/", "", ""))


def lgtm_start_command() -> list[str]:
    """Start command with the two non-secret lifecycle values overlaid."""
    return [
        "/usr/bin/env",
        f"GRAFANA_ROOT_URL={grafana_root_url()}",
        f"AVA_LGTM_PYTHON={sys.executable}",
        "bash",
        "start.sh",
    ]


def remove_obsolete_grafana_root(compose_dir: Path) -> bool:
    """Drop only the retired public-root key from the secret-bearing .env."""
    path = compose_dir / ".env"
    if not path.exists() or not _OBSOLETE_ROOT_ASSIGNMENT.search(path.read_text()):
        return False
    removed, _key = unset_key(path, "GRAFANA_ROOT_URL")
    if not removed:
        raise RuntimeError(f"failed to remove obsolete GRAFANA_ROOT_URL from {path}")
    path.chmod(0o600)
    return True


def _endpoint_answers(url: str) -> bool:
    """Any HTTP answer = the container's listener is up; connection failure = down."""
    try:
        with _NO_PROXY_OPENER.open(url, timeout=2.0):
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
    compose_dir = lgtm_compose_dir(repo)
    remove_obsolete_grafana_root(compose_dir)
    result = subprocess.run(
        lgtm_start_command(),
        cwd=compose_dir,
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
    if "gateway" not in machine_role():
        sys.stderr.write("lgtm-host marker requires the gateway capability on this unit\n")
        sys.exit(1)
    down = down_probes()
    if not down:
        return
    sys.stderr.write(f"lgtm backends down ({', '.join(down)}) — re-running start.sh\n")
    if not _restart_stack():
        sys.exit(1)


if __name__ == "__main__":
    main()
