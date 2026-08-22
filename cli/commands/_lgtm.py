"""LGTM observability-stack lifecycle — converge bring-up + `ava status` view.

The LGTM compose stack (deploy/lgtm: Tempo/Loki/Prometheus/Grafana/promtail)
is the cluster's observability backend and a HOST SINGLETON: fixed host ports
(3003/3100/3200/9090/14318) and one compose project per box. Converge runs on
EVERY `ava start` of every cluster on the host — dev worktree clusters
included — so the bring-up is gated on the `$AVA_HOME/lgtm-host` marker file
(machine-identity-file pattern, operator-created once on the designated host;
see deploy/lgtm/README.md). A home without the marker never touches the
containers. The gateway watchdog's keepalive probe
(services/healthchecks/lgtm.py) is gated on the same marker.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from cli.commands._converge_spec import ConvergeCtx
from services.healthchecks.lgtm import (
    is_lgtm_host,
    lgtm_compose_dir,
    lgtm_host_marker,
    lgtm_start_command,
    probe_statuses,
    remove_obsolete_grafana_root,
)
from shared.machine import machine_role

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

    Marker absent = no-op (this home does not own the host's containers).
    Marker present: run the idempotent deploy/lgtm/start.sh (docker daemon
    bring-up + `docker compose up -d`). A marked host without the docker CLI
    warns and skips (environment limit, same contract as the browser step); a
    failing start.sh propagates — the marker is the operator's statement that
    this host owns the gateway's observability backend, and a silent skip
    would hide its loss.
    """
    if not (ctx.ava_home / "lgtm-host").exists():
        return
    if ctx.roles is None or "gateway" not in ctx.roles:
        raise RuntimeError("lgtm-host marker requires the gateway capability on this unit")
    if shutil.which("docker") is None:
        print(
            "  ! lgtm: docker CLI not found — observability stack not started",
            file=sys.stderr,
        )
        return
    compose_dir = lgtm_compose_dir(ctx.repo)
    remove_obsolete_grafana_root(compose_dir)
    result = subprocess.run(
        lgtm_start_command(),
        cwd=compose_dir,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"deploy/lgtm/start.sh exited {result.returncode}")


def cmd_lgtm_on() -> int:
    """`ava lgtm on` — designate THIS host as the LGTM host and bring the
    stack up. Writes the `$AVA_HOME/lgtm-host` marker (so converge and the
    gateway watchdog keep the stack alive from now on) and runs the idempotent
    deploy/lgtm/start.sh. Safe to re-run; volumes persist across off/on."""
    import cli.commands as _ns

    if "gateway" not in machine_role():
        print("✗ ava lgtm on requires the gateway capability on this unit", file=sys.stderr)
        return 1
    if shutil.which("docker") is None:
        print("✗ docker CLI not found — install docker (OrbStack on macOS) first", file=sys.stderr)
        return 1
    marker = lgtm_host_marker()
    if not marker.exists():
        marker.touch()
        print(f"✓ marker written: {marker}")
    compose_dir = lgtm_compose_dir(_ns._repo_root())
    remove_obsolete_grafana_root(compose_dir)
    result = subprocess.run(
        lgtm_start_command(),
        cwd=compose_dir,
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
    watchdog resurrects the containers within ~a minute), then compose-downs
    the stack. Volumes persist — `ava lgtm on` restores full history.

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
    if shutil.which("docker") is None:
        print("docker CLI not found — nothing to stop")
        return 0
    result = subprocess.run(
        ["bash", "stop.sh"],
        cwd=lgtm_compose_dir(_ns._repo_root()),
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
    """`ava lgtm status` — marker + containers + the four readiness probes."""
    if not is_lgtm_host():
        print(
            "this host is not the LGTM host (no $AVA_HOME/lgtm-host marker) — `ava lgtm on` to designate it"
        )
        return 0
    print_lgtm_status()
    return 0


def print_lgtm_status() -> None:
    """The `ava status` LGTM section: compose containers + the four readiness
    probes. Caller gates on `is_lgtm_host()` — this host runs the compose, so
    loopback + the compose file's fixed host ports are the contract."""
    import cli.commands as _ns

    compose_dir = lgtm_compose_dir(_ns._repo_root())
    if shutil.which("docker") is None:
        print("  ✗ docker CLI not found")
    else:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.Service}}\t{{.Status}}"],
            cwd=compose_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if ps.returncode != 0:
            print(f"  ✗ docker compose ps failed: {ps.stderr.strip() or 'docker daemon down?'}")
        elif not ps.stdout.strip():
            print("  ✗ no containers running (converge on this host runs deploy/lgtm/start.sh)")
        else:
            for line in ps.stdout.strip().splitlines():
                service, _, container_status = line.partition("\t")
                print(f"  {service:<12} {container_status}")
    for name, up in probe_statuses():
        print(f"  {'✓' if up else '✗'} {name} readiness")
