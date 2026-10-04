"""browser-mcp's SIGTERM cleanup budget, importable without the MCP SDK.

`mcp_daemon.run`'s `finally` awaits `SHUTDOWN_STEPS` cleanup steps one after the
other (the upstream stack close, then the listener close); `mcp_upstream._bounded`
cuts each off after `SHUTDOWN_STEP_TIMEOUT_S`. So the daemon may keep closing for
`SHUTDOWN_CEILING_S`, which is longer than ava-root's default TERM window: the
roster declares it as the unit's `stop_ceiling_s`, and root's window is derived
from it, so root never refuses a daemon that is still inside its own bound. Add a
step to that `finally` and this count moves with it.
"""

from __future__ import annotations

SHUTDOWN_STEP_TIMEOUT_S = 10.0
SHUTDOWN_STEPS = 2
SHUTDOWN_CEILING_S = SHUTDOWN_STEPS * SHUTDOWN_STEP_TIMEOUT_S
