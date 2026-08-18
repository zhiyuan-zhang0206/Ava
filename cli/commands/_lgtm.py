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
    probe_statuses,
)

__all__ = ["ensure_lgtm_stack_step", "is_lgtm_host", "print_lgtm_status"]


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
    if shutil.which("docker") is None:
        print(
            "  ! lgtm: docker CLI not found — observability stack not started",
            file=sys.stderr,
        )
        return
    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=lgtm_compose_dir(ctx.repo),
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"deploy/lgtm/start.sh exited {result.returncode}")


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
