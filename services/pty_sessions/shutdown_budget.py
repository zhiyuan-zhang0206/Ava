"""The pty-sessions service's SIGTERM cleanup budget, importable without the service.

A stopping service closes whatever sessions are still alive through the one
terminal closure: hang up, `SHUTDOWN_HANGUP_WAIT_S`, then SIGKILL with each wait
bounded by `SHUTDOWN_KILL_S` (a normal `ava stop` closed its sessions already
and finds none). Several busy sessions take several SIGKILL waits in turn, so
the ceiling is a generous bound rather than the sum: the roster declares it as
the unit's `stop_ceiling_s`, and root's window is derived from it.
"""

from __future__ import annotations

SHUTDOWN_HANGUP_WAIT_S = 2.0
SHUTDOWN_KILL_S = 3.0
SHUTDOWN_CEILING_S = 20.0
