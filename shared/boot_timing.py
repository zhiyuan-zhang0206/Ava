"""The agent boot timing family — the four clocks that order one spawn.

These four numbers used to live as settings defaults in two files
(`shared/config/gateway.py` + `shared/config/daemon.py`), their ordering described
only in prose docstrings and pinned by three tests in `tests/shared/test_config.py`. The orderings are now enforced: `shared/timing.py` registers every clock in this family and `validate_clock_lattice()` asserts the whole lattice (defaults in tests, live values at restarter startup).
They are **not independent**: each pair below is load-bearing, and the chain is
what upgrades the launcher's liveness probe from "the pid exists" to "the boot is
progressing".

## The chain

    BOOT_STALL_SEC (30) < LAUNCH_CONFIRM_TIMEOUT_SEC (45) < BOOT_BUDGET_SEC (90)
        < ALLOCATED_REAP_GRACE_SEC (120)

- **BOOT_STALL_SEC < LAUNCH_CONFIRM_TIMEOUT_SEC**: the child's own boot watchdog
  must fire before the launcher gives up. The launcher treats a live process as a
  progressing boot; that inference is only sound because a stalled child has
  already exited itself by the time the launcher looks. Invert this pair and the
  launcher reaches its deadline while a wedged child still holds a pid, reads it
  as "slow boot on a loaded box", and grants its one extension — spending the
  whole reap grace on a boot that stopped moving long before.
- **LAUNCH_CONFIRM_TIMEOUT_SEC < ALLOCATED_REAP_GRACE_SEC**: the reaper must not
  take a row the launcher is still legitimately waiting on. The 2026-07-30 spawn
  incident is exactly this pair inverted: the confirm window was raised without
  the grace, and the restarter's allocated-reaper started reaping rows from
  launches that were still waiting.
- **BOOT_STALL_SEC < BOOT_BUDGET_SEC**: the stall window bounds ONE phase, so it
  bounds the whole boot only at phases x stall — arithmetic over a number that
  moves whenever a boot phase is added. The budget is the hard ceiling on the
  whole pre-claim boot; a budget below the stall window would make the stall
  window unreachable.
- **BOOT_BUDGET_SEC < ALLOCATED_REAP_GRACE_SEC**: the child must be gone before
  the reaper could claim its row. The reaper's clock is `status_changed_at`,
  which only a status flip resets, so a boot that outlived the grace would have
  its row reaped out from under a live, progressing child — the 2026-07-30
  incident's exact failure, relocated from the launcher to the reaper.

The orderings are enforced, not just documented: `shared/timing.py` registers
every clock in this family and `validate_clock_lattice()` asserts the whole
lattice (defaults in tests, live values at restarter startup).

Values live here as re-exports of the settings fields, so operator env overrides
(`AVA_AGENT_BOOT_STALL_SECONDS`, ...) keep working — including the kill switch
`AVA_AGENT_BOOT_STALL_SECONDS=0` that disarms the child watchdog. Consumers import
from this module, never from `shared.config` directly, so a new consumer is
automatically covered by the lattice. The one intentional exception is
`agent/_boot_deadline.py`, which receives stall/budget on argv from the launcher
and must not import the settings stack during the pre-flip boot.
"""

from __future__ import annotations

from shared.config import settings

# The child's own boot watchdog: how long one boot phase may make no progress
# before the child kills itself (`agent/_boot_deadline.py`). Bounds ONE phase,
# not the boot — see the family docstring.
BOOT_STALL_SEC = settings.gateway.agent_boot_stall_seconds

# The launcher's confirm window: how long `ops.agent_launch` polls the row for
# `allocated -> starting` before force-terminating the launch. Must cover the
# child's whole pre-flip segment (python startup, imports, schema assert,
# placement SELECT).
LAUNCH_CONFIRM_TIMEOUT_SEC = settings.gateway.launch_confirm_timeout_seconds

# The child's second bound: hard ceiling on the whole pre-claim boot, enforced by
# the child watchdog alongside the stall window — whichever comes first.
BOOT_BUDGET_SEC = settings.gateway.agent_boot_budget_seconds

# The restarter's allocated-reaper grace: how long a row may sit 'allocated'
# before it is reaped. Also the ceiling on the launcher's one live-child
# extension (`ops/agent_launch.py`), so the confirm never outlives the point
# where the reaper takes the row.
ALLOCATED_REAP_GRACE_SEC = settings.daemon.allocated_reap_grace_seconds
