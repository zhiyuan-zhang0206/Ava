"""Host cancellation diagnostics: bounded unwind and activity-clock reads.

These bounds limit diagnostic work after explicit cancellation. Graceful
maintenance waits for durable drain and fails on timeout; it does not use an
automatic SIGKILL deadline. Registered in shared/timing.py::CLOCKS.
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
