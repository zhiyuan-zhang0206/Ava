"""build_services() is the SINGLE SOURCE of the watchdog keepalive roster (I-5):
the watchdog derives its per-capability healthcheck list from
`ServiceSpec.healthcheck_module` instead of a second hardcoded list that drifted
(task-maintenance was registered but never kept alive).

These guard the invariants that make the importlib-based derivation safe:
1. Every declared healthcheck_module resolves to a callable `main` — a typo'd
   module string would otherwise crash the watchdog at first tick, since the
   roster is resolved via `importlib.import_module(module).main`.
2. ops declares its healthcheck_module (it runs a real healthcheck; its
   ServiceSpec used to say None, which would have dropped it from the derived
   roster).
3. The derived roster covers exactly the build_services services that declare a
   healthcheck_module + are gated in for the capability (plus the two
   pseudo-checks) — a new service is auto-wired, none is silently dropped.
"""

from __future__ import annotations

import importlib

import pytest

from cli.commands._repo import _services_for_roles_annotated, build_services
from services.watchdog import daemon as wd
from shared.machine import MachineRole


def test_every_healthcheck_module_resolves_to_callable_main() -> None:
    for spec in build_services():
        if spec.healthcheck_module is None:
            continue
        mod = importlib.import_module(spec.healthcheck_module)
        assert callable(getattr(mod, "main", None)), (
            f"{spec.session}: healthcheck_module {spec.healthcheck_module!r} has no callable main()"
        )


def test_ops_declares_its_healthcheck_module() -> None:
    """ops runs a real healthcheck (services.healthchecks.ops); its ServiceSpec must
    declare it, else the build_services-derived roster would stop keeping ops alive."""
    ops = next(s for s in build_services() if s.session == "ops")
    assert ops.healthcheck_module == "services.healthchecks.ops"


def test_watchdogs_declare_no_healthcheck_module() -> None:
    """The two watchdog daemons are the monitors — nothing monitors them, so they
    must stay out of the derived roster (healthcheck_module=None).

    Keyed off the cmd module (services.watchdog.daemon), not the session-name
    suffix: delivery-watchdog is a monitored service whose session legitimately
    ends in "watchdog" but runs its own module with its own healthcheck."""
    watchdogs = [s for s in build_services() if "services.watchdog.daemon" in s.cmd]
    assert watchdogs  # sanity: the two watchdog specs exist
    assert all(s.healthcheck_module is None for s in watchdogs)


@pytest.mark.parametrize("role", ["gateway", "agent-runner"])
def test_derived_roster_covers_all_role_healthchecks(
    role: MachineRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No service that declares a healthcheck_module (and is gated in for this role)
    is dropped from the watchdog roster — the single-source contract. Config-gated
    services are forced on so the comparison is about coverage, not gating."""
    monkeypatch.setattr(wd, "read_skipped", set)
    monkeypatch.setattr(wd.settings.daemon, "heartbeat_enabled", True)
    monkeypatch.setattr(wd.settings.daemon, "task_maintenance_enabled", True)
    monkeypatch.setattr(wd.settings.services, "browser_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)

    expected = {
        spec.session
        for spec, reason in _services_for_roles_annotated(frozenset({role}))
        if spec.healthcheck_module is not None and reason is None
    }
    got = {c.name for c in wd._checks_for_capability(role)}
    # The roster is the derived set plus the gateway-only pseudo-checks (no
    # ServiceSpec: redis and pgbouncer are native per-cluster data-plane processes,
    # lgtm is a docker compose stack, pg-backup is a scheduled job).
    pseudo = {"redis-acl", "pgbouncer", "lgtm", "pg-backup"} if role == "gateway" else set()  # pyright: ignore[reportUnknownVariableType]
    assert got == expected | pseudo
