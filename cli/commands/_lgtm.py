"""Native LGTM lifecycle — local backends, remote Tempo, and status view.

The LGTM host runs Loki and Prometheus as Ava-managed launchd jobs and Grafana
as a host-managed native job. Tempo is remote and configured by the telemetry
endpoint. The local backends are a HOST SINGLETON with fixed ports
(3003/3100/9090), gated on the `$AVA_HOME/lgtm-host` marker so unmarked homes
never touch them. The gateway watchdog's keepalive probe uses the same gate.
"""

from __future__ import annotations

import subprocess
import sys

from cli.commands._converge_spec import ConvergeCtx
from services.healthchecks.lgtm import (
    is_lgtm_host,
    lgtm_deploy_dir,
    lgtm_host_marker,
    probe_statuses,
)

__all__ = [
    "cmd_lgtm_off",
    "cmd_lgtm_on",
    "cmd_lgtm_status",
    "ensure_lgtm_stack_step",
    "is_lgtm_host",
    "print_lgtm_status",
]


def ensure_lgtm_stack_step(ctx: ConvergeCtx) -> None:
    """Converge step: bring up the LGTM stack on the designated LGTM host.

    Marker absent = no-op (this home does not own the host's local backends).
    Marker present: run the idempotent native deploy/lgtm/start.sh. A failing
    start.sh propagates — the marker is the operator's statement that this host
    owns the gateway's observability backend, and a silent skip would hide its
    loss.
    """
    if not (ctx.ava_home / "lgtm-host").exists():
        return
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_deploy_dir(ctx.repo),
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"deploy/lgtm/start.sh exited {result.returncode}")


def cmd_lgtm_on() -> int:
    """`ava lgtm on` — designate THIS host as the LGTM host and bring the
    stack up. Writes the `$AVA_HOME/lgtm-host` marker (so converge and the
    gateway watchdog keep the stack alive from now on), installs current native
    backends, and runs the idempotent deploy/lgtm/start.sh. Safe to re-run."""
    import cli.commands as _ns
    from cli.commands import _lgtm_native
    from shared.paths import ava_home

    marker = lgtm_host_marker()
    if not marker.exists():
        marker.touch()
        print(f"✓ marker written: {marker}")
    repo = _ns._repo_root()
    _lgtm_native.ensure_lgtm_native(repo, ava_home())
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_deploy_dir(repo),
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        print(f"✗ deploy/lgtm/start.sh exited {result.returncode}", file=sys.stderr)
        return 1
    return 0


def cmd_lgtm_off() -> int:
    """`ava lgtm off` — take the observability stack down on this host and
    stop being the LGTM host. Removes the marker FIRST (else the gateway
    watchdog resurrects local backends within ~a minute), then stops the
    native jobs.

    The point of the toggle is measuring observability's own overhead, so it
    prints the caveat that matters for a clean A/B: producers probe the OTLP
    endpoint once at process start, so already-running services keep paying
    export-retry cost until restarted, and services started while OFF stay
    export-disabled until restarted after ON."""
    import cli.commands as _ns

    marker = lgtm_host_marker()
    if marker.exists():
        marker.unlink()
        print(f"✓ marker removed: {marker} (converge/watchdog will no longer touch the stack)")
    result = subprocess.run(
        ["bash", "stop.sh"],
        cwd=lgtm_deploy_dir(_ns._repo_root()),
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        print(f"✗ deploy/lgtm/stop.sh exited {result.returncode}", file=sys.stderr)
        return 1
    print(
        "note: OTLP export is probed once at process start — for a clean\n"
        "overhead A/B, restart the cluster's services after toggling\n"
        "(`ava restart`), in both directions."
    )
    return 0


def cmd_lgtm_status() -> int:
    """`ava lgtm status` — marker + native jobs + local readiness probes."""
    if not is_lgtm_host():
        print(
            "this host is not the LGTM host (no $AVA_HOME/lgtm-host marker) — `ava lgtm on` to designate it"
        )
        return 0
    print_lgtm_status()
    return 0


def print_lgtm_status() -> None:
    """The `ava status` LGTM section: native jobs and local readiness.

    Caller gates on `is_lgtm_host()` — this host owns all fixed backend ports.
    """
    from cli.commands._lgtm_native import backend_pids
    from shared.paths import ava_home

    for name, pid in backend_pids(ava_home() / "lgtm/native").items():
        print(f"  com.ava.{name:<9} {pid or 'not-running'}")
    for name, up in probe_statuses():
        print(f"  {'✓' if up else '✗'} {name} readiness")
