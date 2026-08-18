"""e2e ports -- dynamically acquire free ports from the kernel.

`socket.bind(('127.0.0.1', 0))` lets the kernel allocate; after closing the socket,
immediately give the port to uvicorn / next dev. Theoretical TOCTOU window is
microseconds; zero collisions across concurrent sessions -- lets multiple local e2e
sessions (and e2e jobs on CI runners) each get their own free port without clashing
on fixed values.

`*_PORT` / `*_URL` are allocated once at module import time; within the same worker
process, import order is irrelevant, each test file gets the same set. CI runs e2e
serially with `-n 1`: a single dedicated box runs one complete stack at a time --
for resource determinism (one stack owns the CPU), not to avoid port clashes (under
execnet, each xdist worker is a separate process, each re-importing to get its own
kernel-allocated ports).
"""

from __future__ import annotations

import socket


def _alloc_free_port() -> int:
    """Bind 0 -> kernel allocates free port -> close -> take returned port for downstream."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GATEWAY_PORT = _alloc_free_port()
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"

# Some Next.js versions are strict about cross-origin (127.0.0.1 vs localhost
# count as two origins); Playwright navigate uses localhost to align with next dev
# `Local:` line; backend fetch still uses 127.0.0.1. Gateway CORS covers both
# localhost / 127.0.0.1 sets.
FRONTEND_PORT = _alloc_free_port()
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
