"""The converge step contract — the types every converge step is written against.

Split out of ``_converge.py`` so the step implementations can live in more than
one module without an import cycle: a step module imports this, and
``_converge.py`` imports the step modules. ``_converge`` re-exports these names,
so ``from cli.commands._converge import ConvergeCtx`` keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared.machine import MachineRole, MachineRoles

ALL_ROLES: frozenset[MachineRole] = frozenset({"gateway", "agent-runner", "observability-station"})

# The same capabilities in a deterministic order, for steps that fan out over
# whichever ones a unit carries. A frozenset has no order, and `sorted()` over
# one widens the `MachineRole` literal back to `str`.
CAPABILITY_ORDER: tuple[MachineRole, ...] = ("agent-runner", "gateway")


@dataclass(frozen=True)
class ConvergeCtx:
    repo: Path
    ava_home: Path
    roles: MachineRoles | None  # None = unit not configured yet (fresh install)


@dataclass(frozen=True)
class ConvergeStep:
    name: str
    apply: Callable[[ConvergeCtx], None]
    roles: frozenset[MachineRole] = ALL_ROLES
    requires_unit_config: bool = False
    # host-global wiring (the single `~/.local/bin/ava` symlink + shell-rc PATH
    # edit) belongs to the host's prod install, not to any one cluster. Skipped
    # for a dev / non-default cluster so a worktree's `ava start` never repoints
    # the host's prod `ava` or rewrites the shell rc.
    host_global: bool = False
