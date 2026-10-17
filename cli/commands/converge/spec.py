"""The converge step contract — the types every converge step is written against.

Split out of ``host.py`` so the step implementations can live in more than
one module without an import cycle: a step module imports this, and
``host.py`` imports the step modules. ``host`` re-exports these names,
so ``from cli.commands.lifecycle.converge_host import ConvergeCtx`` keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.agents.context.clients import DatabaseFactory
from base.clock import Timers
from base.cluster.machine import MachineIdentity, MachineRole, MachineRoles
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.telemetry import EventPipeline

ALL_ROLES: frozenset[MachineRole] = frozenset({"gateway", "agent-runner", "observability-station"})

# The same capabilities in a deterministic order, for steps that fan out over
# whichever ones a unit carries. A frozenset has no order, and `sorted()` over
# one widens the `MachineRole` literal back to `str`.
CAPABILITY_ORDER: tuple[MachineRole, ...] = ("agent-runner", "gateway")


@dataclass(frozen=True)
class ConvergeCtx:
    repo: Path
    meiva_home: Path
    roles: MachineRoles | None  # None = unit not configured yet (fresh install)
    config: ConfigBoot
    machine: MachineIdentity
    database_factory: DatabaseFactory
    producer: Callable[[], EventPipeline]
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None
    # never means "skip the helper": a platform that has one always carries it, and a step that
    # needs it calls `require_helper`.
    helper: HelperFactory | None
    host: Host
    timers: Timers
    services: frozenset[str]

    def require_helper(self) -> HelperFactory:
        """The permissions-helper factory, for a step that only runs where a helper exists."""
        if self.helper is None:
            raise RuntimeError(
                "this converge step needs the permissions helper, but this platform has none"
            )
        return self.helper

    def read_config(self) -> ConfigBoot:
        """Read startup inputs on first use, retaining an explicitly prepared owner."""
        if not self.config.prepared:
            self.config.read_process_environment()
        return self.config


@dataclass(frozen=True)
class ConvergeStep:
    name: str
    apply: Callable[[ConvergeCtx], None]
    roles: frozenset[MachineRole] = ALL_ROLES
    requires_unit_config: bool = False
    # host-global wiring (the single `~/.local/bin/meiva` symlink + shell-rc PATH
    # edit) belongs to the host's prod install, not to any one cluster. Skipped
    # for a dev / non-default cluster so a worktree's `meiva start` never repoints
    # the host's prod `meiva` or rewrites the shell rc.
    host_global: bool = False
    # Empty means shared host/cluster preparation. Otherwise one selected
    # consumer is required; role membership alone does not select a service.
    services: frozenset[str] = frozenset()
