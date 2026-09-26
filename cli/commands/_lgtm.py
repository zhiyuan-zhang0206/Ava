"""Observability desired state; all backend lifecycle uses normal start and root."""

from __future__ import annotations

from cli.commands._converge_spec import ConvergeCtx
from services.healthchecks.lgtm import is_lgtm_host, lgtm_host_marker, probe_backend
from shared.lgtm_local import BACKENDS
from shared.machine import MachineRoles


def roles_declare_station(roles: MachineRoles | None) -> bool:
    return roles is not None and "observability-station" in roles


def is_station_ctx(ctx: ConvergeCtx) -> bool:
    return (ctx.ava_home / "lgtm-host").exists() or roles_declare_station(ctx.roles)


def _reconcile(*, enabled: bool) -> int:
    """Change only backend intent; use the same lifecycle as every service."""
    from cli.commands.start import cmd_start
    from shared.service_selection import read_selection

    selection = read_selection()
    names = set(selection.names)
    if (selection.mode == "only") == enabled:
        names.update(BACKENDS)
    else:
        names.difference_update(BACKENDS)
    if selection.mode == "only":
        # Turning off an allowlist containing only LGTM leaves an explicit
        # empty desired tree, not the default all-services selection.
        if not names:
            from ops.roster import build_services

            return cmd_start(disabled_services=tuple(s.session for s in build_services()))
        return cmd_start(only_services=tuple(sorted(names)))
    return cmd_start(disabled_services=tuple(sorted(names)), all_services=not names)


def cmd_lgtm_on() -> int:
    """Declare this home an observability station and reconcile normal start."""
    marker = lgtm_host_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    return _reconcile(enabled=True)


def cmd_lgtm_off() -> int:
    """Disable the backend units, preserving observation data and other services."""
    lgtm_host_marker().unlink(missing_ok=True)
    return _reconcile(enabled=False)


def cmd_lgtm_status() -> int:
    if not is_lgtm_host():
        print("this home is not an observability station")
    print_lgtm_status()
    return 0


def print_lgtm_status() -> None:
    for name in BACKENDS:
        result = probe_backend(name)
        print(f"  {name}: {result.verdict.value} ({result.detail})")
