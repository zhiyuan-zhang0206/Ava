"""Subprocess exit code constants.

Standalone top-level module (not under ava/) — because
ava/__init__.py triggers `_AvaEnv()` to read env and initialize
DB/Redis connections, which is only valid in a subprocess; importing
from an agent loop (agent/) process would blow up. Exit code constants
themselves have no env dependency; putting them at the top level lets
both sides import without triggering ava SDK initialization side
effects.
"""

IDLE_EXIT_CODE = 42  # `ava.self.terminate()` / `ava.self.restart()`: this turn ends (halted=True)
SYSTEM_HALT_EXIT_CODE = 43  # `ava.self.compact(summary)`: agent altered control flow

# `ava restart` / `ava cluster update` refused BEFORE stopping anything: its
# validate-before-kill preflight failed, so the host is untouched and still serving.
# Read by the detached updater shell, which must recover a host that is DOWN
# (`ava start`) and must NOT `ava start` over one that is deliberately still up.
# Distinct from a generic failure (1) precisely because "may be down" is the safe
# reading of any *other* non-zero code.
RESTART_DECLINED_EXIT_CODE = 3

# `ava start` / `ava restart` ran every step successfully and launched this host's
# services, but at least one of them never passed its liveness probe within
# `shared.deploy_timing.SERVICE_READY_TIMEOUT_S`. The status snapshot printed just
# before the exit names which (`cli.commands._probe`).
#
# Its own code rather than 1, because the two ask a program to do different things.
# 1 means a start STEP failed — converge, the data plane, migrations, the schema
# assertion, machine registration — so the host may have no services at all and
# nothing about it is trustworthy. This code means the sequence completed and the
# host is up but incompletely: retrying `ava start` is idempotent and reasonable,
# and the watchdog's keepalive is already armed to revive the stragglers.
#
# It must NOT be 3: the detached updater's recovery ladders
# (`ops.cluster_deploy._RESTART_RECOVERY_SH` / `_RESTART_RECOVERY_CMD`) read 3 as
# "nothing was stopped, host still serving -> do NOT start over it". A restart that
# stopped services and came back with one not serving is the opposite of that, and
# routing it into the decline branch would leave a half-down host untouched. Being
# above `RESTART_DECLINED_EXIT_CODE` puts it in those ladders' "may be down ->
# `ava start`" branch, which is the correct and idempotent response.
SERVICES_NOT_READY_EXIT_CODE = 4

# A temporary pause/stop did not finish. Source/schema were not advanced by its
# caller, but some services may already have stopped; never report rollback success.
STOP_INCOMPLETE_EXIT_CODE = 5
