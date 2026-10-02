"""The standard-daemon entries render exactly as the hand-written ones did.

Eleven roster entries moved from a full `ServiceSpec(...)` to `healthz_daemon(...)`.
`ava-root` refuses to reuse a running generation whose unit digest changed (the digest
binds the exec argv, environment, inputs and stop window), and the launcher derives a
profile marker, a database login class and an API token class from each spec, so "the
same service" means the same render and the same derived classes — not merely similar
fields. This file keeps the pre-migration definitions as written and requires the live
roster to render identically to them, row by row, through the real renderer.

What is deliberately NOT compared: `healthcheck_module` (the eleven probe modules it
named had no importer; root probes through `identity_probe`), the `pidfile` attribute
three of them carried (read by nothing), and the new `health_name`. When these
definitions stop being useful as a lock, delete the file; nothing else depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineRole
from base.daemon.health import DaemonProbe, probe_daemon
from base.paths import pid_path
from ops import roster
from ops.roster import _bind_owned_probe
from ops.roster.healthz import healthz_url
from ops.roster.service_spec import DbAccess, ServiceSpec, api_access, db_access, profile_marker
from services.ava_root_glue import manifests as gen

_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_REPO = Path("/checkout/repo")


@dataclass(frozen=True)
class _Legacy:
    """One pre-migration definition: the facts the hand-written `ServiceSpec` carried."""

    session: str
    module: str
    capabilities: frozenset[MachineRole]
    requires_db: bool
    profile: str | None = None
    no_profile_marker: bool = False
    db_access: str | None = None
    gated: bool = False


_LEGACY = (
    _Legacy("im-bridge", "services.im_bridge.daemon", _GATEWAY, True),
    _Legacy(
        "labeler",
        "services.labeler.daemon",
        _GATEWAY,
        True,
        no_profile_marker=True,
    ),
    _Legacy("heartbeat", "services.heartbeat.daemon", _GATEWAY, True),
    _Legacy(
        "delivery-watchdog",
        "services.delivery_watchdog.daemon",
        _GATEWAY,
        True,
    ),
    _Legacy(
        "events-maintenance",
        "services.events_maintenance.daemon",
        _GATEWAY,
        True,
    ),
    _Legacy("pg-backup", "services.backup_scheduler.daemon", _GATEWAY, True),
    _Legacy("ttl-reaper", "services.ttl_reaper.daemon", _GATEWAY, True),
    _Legacy("schedule-manager", "services.schedule_manager.daemon", _GATEWAY, True),
    _Legacy("page-server", "services.page_server.daemon", _RUNNER, True),
    _Legacy(
        "agent-host",
        "services.agent_host.daemon",
        _RUNNER,
        True,
        profile="agent",
    ),
    _Legacy("ops", "services.agent_ops.daemon", _RUNNER, True),
    _Legacy(
        "task-maintenance",
        "ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
        _GATEWAY,
        True,
        gated=True,
    ),
    _Legacy(
        "memory-indexer",
        "services.memory_indexer.daemon",
        _GATEWAY,
        False,
        db_access="gateway",
        gated=True,
    ),
)


def _legacy_spec(row: _Legacy) -> ServiceSpec:
    """The definition as it was written: every derived field spelled out by hand."""
    name = row.session.replace("-", "_")
    return ServiceSpec(
        session=row.session,
        cmd=f".venv/bin/python -m {row.module}",
        capabilities=row.capabilities,
        requires_db=row.requires_db,
        curl_url=healthz_url(name),
        identity_probe=partial(probe_daemon, name, healthz_url(name), pidfile=pid_path(name)),
        profile=row.profile,
        no_profile_marker=row.no_profile_marker,
        db_access=cast("DbAccess | None", row.db_access),
        gate=(lambda: None) if row.gated else None,
    )


def _describe(probe: object) -> object:
    """A comparable shape for a probe (`functools.partial` has no equality)."""
    if isinstance(probe, partial):
        inner = cast("partial[DaemonProbe]", probe)
        return (
            _describe(inner.func),
            tuple(_describe(a) for a in inner.args),
            {k: _describe(v) for k, v in sorted(inner.keywords.items())},
        )
    if callable(probe):
        return f"{getattr(probe, '__module__', '?')}.{getattr(probe, '__qualname__', '?')}"
    return probe


def _live() -> dict[str, ServiceSpec]:
    return {s.session: s for s in roster.build_services()}


def test_the_migrated_sessions_are_exactly_the_standard_daemons() -> None:
    assert {r.session for r in _LEGACY} == {
        s for s, spec in _live().items() if spec.health_name is not None
    }


@pytest.mark.parametrize("row", _LEGACY, ids=lambda r: r.session)
def test_the_rendered_unit_is_unchanged(row: _Legacy) -> None:
    live = _live()[row.session]
    # The legacy side goes through the same root-ownership binding the roster applies.
    old = _bind_owned_probe(_legacy_spec(row))
    old_units = gen.build_units([old], capabilities=row.capabilities, repo_root=_REPO)
    new_units = gen.build_units([live], capabilities=row.capabilities, repo_root=_REPO)

    assert new_units == old_units


@pytest.mark.parametrize("row", _LEGACY, ids=lambda r: r.session)
def test_everything_the_launcher_derives_is_unchanged(row: _Legacy) -> None:
    live = _live()[row.session]
    old = _bind_owned_probe(_legacy_spec(row))

    assert (live.session, live.cmd, live.capabilities, live.requires_db) == (
        old.session,
        old.cmd,
        old.capabilities,
        old.requires_db,
    )
    assert (live.curl_url, live.tcp_port, live.stop_ceiling_s) == (
        old.curl_url,
        old.tcp_port,
        old.stop_ceiling_s,
    )
    assert (live.profile, live.no_profile_marker, live.db_access, live.config_inputs) == (
        old.profile,
        old.no_profile_marker,
        old.db_access,
        old.config_inputs,
    )
    assert (profile_marker(live), db_access(live), api_access(live)) == (
        profile_marker(old),
        db_access(old),
        api_access(old),
    )
    assert (live.gate is None) == (old.gate is None)
    assert _describe(live.identity_probe) == _describe(old.identity_probe)
