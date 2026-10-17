from __future__ import annotations

from base.cluster.machine import MachineIdentity, MachineRoles
from base.paths import meiva_home
from base.telemetry.lgtm_local import BACKENDS, backend_urls
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability._lgtm_provisioning import verify_dashboard_provisioning
from services.supervision.healthchecks.lgtm import is_lgtm_host, probe_backend


def roles_declare_station(roles: MachineRoles | None) -> bool:
    return roles is not None and "observability-station" in roles


def is_station_ctx(ctx: ConvergeCtx) -> bool:
    return (ctx.meiva_home / "lgtm-host").exists() or roles_declare_station(ctx.roles)


# meiva:ref cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md#dashboard-provisioning-cutover
def cmd_lgtm_status(*, machine: MachineIdentity) -> int:
    station = is_lgtm_host(machine=machine)
    if not station:
        print("this home is not an observability station")
    print_lgtm_status()
    if not station:
        return 0
    problems = verify_dashboard_provisioning(
        meiva_home() / "lgtm" / "native", grafana_url=backend_urls()["grafana"]
    )
    for problem in problems:
        print(f"  dashboard: {problem}")
    if not problems:
        print("  dashboard: meiva-ops-main served from folder Meiva")
    return 1 if problems else 0


def print_lgtm_status() -> None:
    for name in BACKENDS:
        result = probe_backend(name)
        print(f"  {name}: {result.verdict.value} ({result.detail})")
