"""Native LGTM lifecycle — local backends, remote Tempo, and status view.

The observability station owns Loki, Prometheus, and Grafana: launchd on
Darwin arm64, user systemd on Linux amd64. Tempo remains remote. Native
service identities include the home path, with explicit host listen ports.
The marker or observability-station capability gates automatic lifecycle work.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from services.healthchecks.lgtm import (
    is_lgtm_host,
    lgtm_deploy_dir,
    lgtm_host_marker,
    probe_statuses,
)
from shared.lgtm_local import lifecycle_environment
from shared.machine import MachineRoles

__all__ = [
    "cmd_lgtm_off",
    "cmd_lgtm_on",
    "cmd_lgtm_status",
    "ensure_lgtm_stack_step",
    "is_lgtm_host",
    "is_station_ctx",
    "print_lgtm_status",
    "roles_declare_station",
]


def is_station_ctx(ctx: ConvergeCtx) -> bool:
    """Whether this converge context owns the host's local LGTM backends.

    Provider identity is either form: the legacy `$AVA_HOME/lgtm-host` marker
    (operator designation, `ava lgtm on`) or the declarative
    `observability-station` unit capability. A role-declared station renders and
    brings up the stack with no marker; the marker path stays byte-for-byte
    intact (zero regression for the existing LGTM host).
    """
    return (ctx.ava_home / "lgtm-host").exists() or (
        ctx.roles is not None and "observability-station" in ctx.roles
    )


def roles_declare_station(roles: MachineRoles | None) -> bool:
    """Whether a capability set carries the observability-station token."""
    return roles is not None and "observability-station" in roles


def _start_stack(repo: Path, home: Path) -> int:
    """Use the same host lifecycle from converge and the explicit on command."""
    if platform.system() == "Linux":
        from shared.lgtm_systemd import start

        start(home)
        return 0
    return subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_deploy_dir(repo),
        env=lifecycle_environment(),
        check=False,
        timeout=600,
    ).returncode


def ensure_lgtm_stack_step(ctx: ConvergeCtx) -> None:
    """Converge step: bring up the LGTM stack on the observability station.

    Neither marker nor capability = no-op (this home does not own the host's
    local backends). Otherwise run the idempotent native deploy/lgtm/start.sh.
    A failing start.sh propagates — the station identity is the operator's
    statement that this host owns the gateway's observability backend, and a
    silent skip would hide its loss.
    """
    if not is_station_ctx(ctx):
        return
    result = _start_stack(ctx.repo, ctx.ava_home)
    if result != 0:
        raise RuntimeError(f"deploy/lgtm/start.sh exited {result}")


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
    result = _start_stack(repo, ava_home())
    if result != 0:
        print(f"✗ deploy/lgtm/start.sh exited {result}", file=sys.stderr)
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
    from cli.commands._lgtm_native import bootout_native_jobs
    from shared.paths import ava_home

    marker = lgtm_host_marker()
    if marker.exists():
        marker.unlink()
        print(f"✓ marker removed: {marker} (converge/watchdog will no longer touch the stack)")
    bootout_native_jobs(ava_home())
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

    Caller gates on `is_lgtm_host()` — this home owns the configured backend listeners.
    """
    from cli.commands._lgtm_native import backend_pids
    from shared.paths import ava_home

    for name, pid in backend_pids(ava_home() / "lgtm/native").items():
        print(f"  com.ava.{name:<9} {pid or 'not-running'}")
    for name, up in probe_statuses():
        print(f"  {'✓' if up else '✗'} {name} readiness")
