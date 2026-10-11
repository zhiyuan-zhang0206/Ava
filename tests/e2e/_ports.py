"""e2e ports -- dynamically acquire free ports from the kernel.

The gateway socket stays bound in the pytest worker through frontend build and
every gateway restart. Uvicorn inherits its descriptor; no bind-close-rebind gap.
The frontend still uses a released kernel allocation because Next has no inherited
socket interface here; that separate limitation is not a gateway ownership claim.

`*_PORT` / `*_URL` are allocated once at module import time; within the same worker
process, import order is irrelevant, each test file gets the same set. CI runs e2e
serially with `-n 1`: a single dedicated box runs one complete stack at a time --
for resource determinism (one stack owns the CPU), not to avoid port clashes (under
execnet, each xdist worker is a separate process, each re-importing to get its own
kernel-allocated ports).
"""

from __future__ import annotations

import atexit
import socket


def _alloc_free_port() -> int:
    """Bind 0 -> kernel allocates free port -> close -> take returned port for downstream."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GATEWAY_SOCKET = socket.socket()
GATEWAY_SOCKET.bind(("127.0.0.1", 0))
atexit.register(GATEWAY_SOCKET.close)
GATEWAY_PORT = GATEWAY_SOCKET.getsockname()[1]
# `localhost`, like the frontend and like a started cluster's own gateway URL: the
# gateway authenticates the browser with a SameSite=Lax session cookie, which a page
# on `localhost` sends to another `localhost` port (same site) and not to `127.0.0.1`.
# The retained listener binds 127.0.0.1, which is what `localhost` resolves to for IPv4.
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"

# Playwright navigates on `localhost` to align with next dev's `Local:` line.
# Gateway CORS covers both localhost / 127.0.0.1 sets.
FRONTEND_PORT = _alloc_free_port()
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
