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

Once all listeners answer, pushes a unique Loki probe line and queries it back.
Three consecutive write/read failures re-run start.sh; the counter lives under
AVA_HOME because the watchdog launches a fresh process for every 60-second round.

The stack is the cluster's observability backend: while it is down the
gateway's /ops + inspect reads (Loki/Prometheus), the Grafana-evaluated ops
alerts, and the events-maintenance Loki rollup all degrade. Telemetry is not
lost meanwhile — the native sidecar buffers in its file-backed queue.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from shared import telemetry
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import ava_home

_NANOSECONDS_PER_SECOND = 1_000_000_000
_WRITE_PROBE_LOOKBACK_SECONDS = 120
_WRITE_PROBE_END_LAG_SECONDS = 1
_WRITE_PROBE_RESTART_THRESHOLD = 3


def readiness_probes() -> tuple[tuple[str, str], ...]:
    """The locally managed backends' readiness endpoints, derived from the
    observability settings' base URLs (defaults keep the historical loopback +
    fixed-port contract). A trailing slash on a base URL is stripped before the
    probe path is appended — without it a configured
    ``http://host:port/`` would probe ``//ready`` and get a 404 counted as
    alive (any HTTP answer is alive; only connection failure means down). The
    marker pins this check to the owner host. Remote Tempo intentionally has no
    readiness probe here."""
    return (
        ("loki", f"{settings.observability.telemetry_loki_url.rstrip('/')}/ready"),
        ("prometheus", f"{settings.observability.telemetry_prometheus_url.rstrip('/')}/-/ready"),
        ("grafana", f"{settings.observability.telemetry_grafana_url.rstrip('/')}/api/health"),
    )


def lgtm_host_marker() -> Path:
    """The machine-identity marker that designates THIS host as the one running
    the local LGTM backends (`$AVA_HOME/lgtm-host`, operator-created once)."""
    return ava_home() / "lgtm-host"


def is_lgtm_host() -> bool:
    """Whether this host is the observability station: the `lgtm-host` marker
    OR the declarative `observability-station` unit capability."""
    from shared.observability import home_is_observability_station

    return home_is_observability_station(ava_home())


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
    return [(name, _endpoint_answers(url)) for name, url in readiness_probes()]


def down_probes() -> list[str]:
    """Names of the backends whose readiness endpoint did not answer at all."""
    return [name for name, up in probe_statuses() if not up]


def write_path_probe() -> tuple[bool, str]:
    """Push one unique Loki line and verify that its write path made it queryable."""
    now_ns = time.time_ns()
    marker_ns = now_ns - (_WRITE_PROBE_END_LAG_SECONDS * _NANOSECONDS_PER_SECOND)
    marker = f"watchdog-write-probe-{marker_ns}"
    base_url = settings.observability.telemetry_loki_url.rstrip("/")
    body = json.dumps(
        {
            "streams": [
                {
                    "stream": {"probe_id": "watchdog-write"},
                    "values": [[str(marker_ns), marker]],
                }
            ]
        }
    ).encode()
    push_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/loki/api/v1/push",
        data=body,
        headers={"Content-Type": "application/json", "X-Scope-OrgID": "fake"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(push_request, timeout=2.0) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                return False, f"push_http_{response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"push_http_{exc.code}"
    except Exception:
        return False, "push_error"

    # The range end is exclusive, so it must sit strictly past the pushed
    # line's timestamp. Capture a fresh now after the push (never the
    # pre-push marker time) and anchor the window on the marker instead.
    query_end_ns = time.time_ns()
    query = urllib.parse.urlencode(
        {
            "query": '{probe_id="watchdog-write"}',
            "start": str(marker_ns - (_WRITE_PROBE_LOOKBACK_SECONDS * _NANOSECONDS_PER_SECOND)),
            "end": str(query_end_ns),
        }
    )
    query_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/loki/api/v1/query_range?{query}",
        headers={"X-Scope-OrgID": "fake"},
    )
    try:
        with urllib.request.urlopen(query_request, timeout=2.0) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                return False, "query_error"
            payload: dict[str, Any] = json.loads(response.read())
            visible = any(
                value[1] == marker
                for stream in payload["data"]["result"]
                for value in stream["values"]
            )
    except Exception:
        return False, "query_error"
    return (True, "ok") if visible else (False, "probe_not_visible")


def _write_probe_counter_path() -> Path:
    return ava_home() / "lgtm-write-probe-consecutive-failures"


def _read_counter() -> int:
    try:
        return int(_write_probe_counter_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return 0


def _write_counter(consecutive_failures: int) -> None:
    """Advisory state: a failed write (e.g. a full disk — the exact
    failure this check exists to catch) must not crash the healthcheck
    round; a lost increment only delays the restart verdict by one round."""
    try:
        _write_probe_counter_path().write_text(str(consecutive_failures), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"lgtm write-probe counter write failed: {exc}\n")


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
        # start.sh reports gate failures (e.g. loki -verify-config rejection)
        # through its stdout log lines; keep both streams so the watchdog path
        # surfaces the reason instead of swallowing it.
        sys.stderr.write(
            f"lgtm start.sh failed (exit {result.returncode}): {result.stdout} {result.stderr}\n"
        )
    return result.returncode == 0


def main() -> None:
    init_gateway_process("lgtm")
    if not is_lgtm_host():
        return  # not the designated LGTM host — nothing to keep alive here
    down = down_probes()
    if down:
        sys.stderr.write(f"lgtm backends down ({', '.join(down)}) — re-running start.sh\n")
        restarted = _restart_stack()
        _write_counter(0)
        if not restarted:
            sys.exit(1)
        return

    healthy, reason = write_path_probe()
    if healthy:
        _write_counter(0)
        return

    consecutive_failures = _read_counter() + 1
    _write_counter(consecutive_failures)
    telemetry.emit(
        "telemetry",
        "loki_write_path_probe_failed",
        level="warning",
        source="system",
        attributes={"consecutive_failures": consecutive_failures, "reason": reason},
    )
    if consecutive_failures < _WRITE_PROBE_RESTART_THRESHOLD:
        return
    sys.stderr.write(
        f"lgtm write path probe failed {consecutive_failures} consecutive rounds "
        f"({reason}) — re-running start.sh\n"
    )
    _restart_stack()
    _write_counter(0)


if __name__ == "__main__":
    main()
