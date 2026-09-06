"""Hybrid LGTM healthcheck — called every 60s by the gateway watchdog.

Runs only on the designated LGTM host: the `$AVA_HOME/lgtm-host` marker file
(operator-created once, machine-identity-file pattern — see
deploy/lgtm/README.md) gates this check the same way it gates the converge
bring-up. Without the marker the check is a no-op, so a dev worktree
cluster's watchdog never touches an unassigned backend home. Native units
and their explicitly configured listeners belong to the designated home.

Probes the three LOCAL readiness endpoints: Loki /ready, Prometheus /-/ready,
and Grafana /api/health. Tempo is a remote WSL service, so it is deliberately
outside this repair loop: its failure must not restart local backends. Any HTTP
answer counts as alive (a 503 is a warming-up backend, not a dead one); only a
connection-level failure means a local backend is down, and then the fix is
re-running the idempotent deploy/lgtm/start.sh. Same connection-level contract
as the otel_collector sidecar check.

Once all listeners answer, sends a unique Loki OTLP log and queries it back.
Three consecutive generic write/read failures re-run start.sh. A stuck ingester
is force-restarted immediately once its storage disk drops below the configured
WAL throttle. The counter lives under AVA_HOME because the watchdog launches a
fresh process for every 60-second round.

The stack is the cluster's observability backend: while it is down the
gateway's /ops + inspect reads (Loki/Prometheus), the Grafana-evaluated ops
alerts, and the events-maintenance Loki rollup all degrade. Telemetry is not
lost meanwhile — the native sidecar buffers in its file-backed queue.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import shared.cluster
import shared.lgtm_systemd
import shared.proc
from shared import telemetry
from shared.config import settings
from shared.lgtm_local import lifecycle_environment
from shared.log import init_gateway_process
from shared.loki_index_labels import WAL_DISK_FULL_THRESHOLD
from shared.paths import ava_home

_local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NANOSECONDS_PER_SECOND = 1_000_000_000
_WRITE_PROBE_LOOKBACK_SECONDS = 120
_WRITE_PROBE_END_LAG_SECONDS = 1
_WRITE_PROBE_RESTART_THRESHOLD = 3


def readiness_probes() -> tuple[tuple[str, str], ...]:
    """Probe this home's native bind settings, independent of remote query URLs.

    Tempo is remote and cannot trigger a local backend restart.
    """
    from shared.lgtm_local import backend_urls

    urls = backend_urls()
    return (
        ("loki", f"{urls['loki']}/ready"),
        ("prometheus", f"{urls['prometheus']}/-/ready"),
        ("grafana", f"{urls['grafana']}/api/health"),
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
        with _local_http.open(url, timeout=2.0):
            return True
    except urllib.error.HTTPError:
        return True  # any status proves the listener answered
    except Exception:
        return False


def probe_statuses() -> list[tuple[str, bool]]:
    """(backend name, listener answered) for each local readiness probe."""
    statuses = [(name, _endpoint_answers(url)) for name, url in readiness_probes()]
    if platform.system() == "Linux":
        from shared.lgtm_systemd import running_pid

        return [(name, up and running_pid(ava_home(), name) is not None) for name, up in statuses]
    return statuses


def down_probes() -> list[str]:
    """Names of the backends whose readiness endpoint did not answer at all."""
    return [name for name, up in probe_statuses() if not up]


def write_path_probe() -> tuple[bool, str]:
    """Send one unique OTLP log and verify that Loki made it queryable."""
    now_ns = time.time_ns()
    marker_ns = now_ns - (_WRITE_PROBE_END_LAG_SECONDS * _NANOSECONDS_PER_SECOND)
    marker = f"watchdog-write-probe-{marker_ns}"
    from shared.lgtm_local import backend_urls

    base_url = backend_urls()["loki"]
    body = json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "agent_id", "value": {"stringValue": marker}},
                            {
                                "key": "event_name",
                                "value": {"stringValue": "watchdog-write-probe"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": str(marker_ns),
                                    "body": {"stringValue": marker},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    ).encode()
    push_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/otlp/v1/logs",
        data=body,
        headers={"Content-Type": "application/json", "X-Scope-OrgID": "fake"},
        method="POST",
    )
    try:
        with _local_http.open(push_request, timeout=2.0) as response:
            if not 200 <= response.status < 300:
                response_body = response.read() if response.status >= 500 else b""
                return False, _push_failure_reason(response.status, response_body)
    except urllib.error.HTTPError as exc:
        response_body = exc.read() if exc.code >= 500 else b""
        return False, _push_failure_reason(exc.code, response_body)
    except Exception:
        return False, "push_error"

    # The range end is exclusive, so it must sit strictly past the pushed
    # line's timestamp. Capture a fresh now after the push (never the
    # pre-push marker time) and anchor the window on the marker instead.
    query_end_ns = time.time_ns()
    query = urllib.parse.urlencode(
        {
            "query": f'{{agent_id="{marker}"}}',
            "start": str(marker_ns - (_WRITE_PROBE_LOOKBACK_SECONDS * _NANOSECONDS_PER_SECOND)),
            "end": str(query_end_ns),
        }
    )
    query_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{base_url}/loki/api/v1/query_range?{query}",
        headers={"X-Scope-OrgID": "fake"},
    )
    try:
        with _local_http.open(query_request, timeout=2.0) as response:
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


def _push_failure_reason(status: int, response_body: bytes) -> str:
    if status >= 500 and b"ingester is shutting down" in response_body.lower():
        return "ingester_shutting_down"
    return f"push_http_{status}"


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


def _loki_storage_dir(home: Path) -> Path:
    configured = settings.observability.lgtm_storage_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (home / "lgtm" / "native" / "data").resolve()


def _loki_storage_disk_below_threshold(home: Path) -> bool:
    storage_dir = _loki_storage_dir(home)
    try:
        usage = shutil.disk_usage(storage_dir)
    except OSError as exc:
        sys.stderr.write(f"lgtm Loki storage disk usage probe failed for {storage_dir}: {exc}\n")
        return False
    return usage.used / usage.total < WAL_DISK_FULL_THRESHOLD


def _force_restart_loki(home: Path) -> bool:
    """Restart Loki even when its listener remains responsive; never raise."""
    try:
        system = platform.system()
        if system == "Linux":
            shared.lgtm_systemd.force_restart(home, "loki")
            return True
        if system != "Darwin":
            sys.stderr.write(f"lgtm Loki force restart unsupported on {system}\n")
            return False
        label = f"com.ava.loki.{shared.cluster.home_slug(home)}"
        result = shared.proc.run_bounded(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            timeout=45,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            sys.stderr.write(
                f"lgtm Loki kickstart failed (exit {result.returncode}): {result.stderr}\n"
            )
            return False
        return True
    except Exception as exc:
        sys.stderr.write(f"lgtm Loki force restart failed: {exc}\n")
        return False


def _restart_stack() -> bool:
    """Re-run start.sh; its probe-first path never restarts a live backend."""
    if platform.system() == "Linux":
        from shared.lgtm_systemd import start

        start(ava_home())
        return True
    repo = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_deploy_dir(repo),
        env=lifecycle_environment(),
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
    if reason == "ingester_shutting_down":
        home = ava_home()
        if _loki_storage_disk_below_threshold(home):
            sys.stderr.write(
                "lgtm write path found a stuck ingester below the WAL disk threshold "
                "— force-restarting Loki\n"
            )
            _force_restart_loki(home)
            _write_counter(0)
        return
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
