"""Stop-path timing bounds — one lattice family (issue #196).

The hosted agent-runner's shutdown waits are bounded from above by the stop
path's force-kill window: a cancelled turn gets `CANCEL_UNWIND_TIMEOUT_S` to
unwind, the stuck-agent activity-clock read gets `CLOCK_READ_TIMEOUT_S`, and
the sum of the two plus the rest of shutdown has to fit inside
`REAP_KILL_WINDOW_S` — the SIGTERM-then-SIGKILL window `ava stop` grants agent
sessions. If it does not, the host is killed before it can emit
`host_turn_uncancellable`, and the one diagnostic that says which agent was
wedged is lost precisely when it is needed.

Registered in `shared/timing.py::CLOCKS` with the
`CANCEL_UNWIND_TIMEOUT_S + CLOCK_READ_TIMEOUT_S < REAP_KILL_WINDOW_S`
constraint; `scripts/lint_clock_lattice.py` treats this module as a family
module, so lattice vocabulary may live here and only here.
"""

from __future__ import annotations

# How long a cancelled turn gets to unwind before the host reports it as
# uncancellable and stops waiting (services/agent_host/dispatcher.py).
CANCEL_UNWIND_TIMEOUT_S = 5.0

# Ceiling on reading the stuck agents' activity clocks to enrich that report
# (services/agent_host/dispatcher.py). Small on purpose: diagnostic enrichment
# of a report already worth emitting without it, and the DB may be exactly what
# a wedged turn is stuck on.
CLOCK_READ_TIMEOUT_S = 2.0

# The stop path's graceful-then-force window for agent sessions: SIGTERM, then
# SIGKILL stragglers after this (cli/commands/stop.py:_reap_agent_sessions).
REAP_KILL_WINDOW_S = 15.0
