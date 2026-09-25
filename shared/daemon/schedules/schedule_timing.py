"""Schedule-supervision timing — one lattice family.

The gateway's schedule manager raises a missing-session alert when an enabled,
non-completed schedule has gone sessionless for too long. The alert threshold
must outlive a legitimate cluster rollout's no-progress window: otherwise an
ordinary stop-the-world rollout would produce a schedule-stalled alert before
the updater itself is judged stuck.

Registered in `shared/timing.py::CLOCKS` with the
`NO_PROGRESS_TIMEOUT_S < SCHEDULE_STALL_ALERT_AFTER_S` constraint;
`scripts/lint_clock_lattice.py` treats this module as a family module, so
lattice vocabulary may live here and only here.

Deliberately separate from `shared/timing.py`: the schedule manager runs under
the gateway process profile, and importing the lattice module would drag its
agent/sandbox-domain settings reads into the gateway closure
(tests/shared/test_gateway_consumer_guard.py enforces the matrix).
"""

from __future__ import annotations

# How long an enabled, non-completed schedule may remain sessionless before
# the manager raises the stall alert (gateway/schedule_manager.py). Must stay
# above NO_PROGRESS_TIMEOUT_S — see the module docstring for why.
SCHEDULE_STALL_ALERT_AFTER_S = 2 * 60 * 60
