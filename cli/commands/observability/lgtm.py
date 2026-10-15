from __future__ import annotations

from base.cluster.machine import MachineIdentity, MachineRoles
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.converge.spec import ConvergeCtx
from services.supervision.healthchecks.lgtm import is_lgtm_host, probe_backend


def roles_declare_station(roles: MachineRoles | None) -> bool:
    return roles is not None and "observability-station" in roles


def is_station_ctx(ctx: ConvergeCtx) -> bool:
    return (ctx.ava_home / "lgtm-host").exists() or roles_declare_station(ctx.roles)


def cmd_lgtm_status(*, machine: MachineIdentity) -> int:
    if not is_lgtm_host(machine=machine):
        print("this home is not an observability station")
    print_lgtm_status()
    return 0


def print_lgtm_status() -> None:
    for name in BACKENDS:
        result = probe_backend(name)
        print(f"  {name}: {result.verdict.value} ({result.detail})")
