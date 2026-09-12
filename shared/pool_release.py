"""Release idle connections from a pool without closing the pool itself.

`psycopg_pool` has no public "close the idle connections, keep the pool usable"
operation: `drain()` closes them and immediately opens replacements (it exists
for reconfiguration), and `close()` is terminal. A stop window needs the third
thing — release now, reconnect lazily on the first borrow after resume — so this
module performs, under the pool's own lock, the mutation `_shrink_pool`
performs one connection at a time: the idle set empties, the connection count
drops with it, and a later borrow grows the pool again through the stock
maintenance task.

Pinned to the psycopg-pool release `uv.lock` holds (3.3.1): both flavors share
one private face — `_lock`, `_pool`, `_nconns`, `_nconns_min`,
`_close_connection` (`pool.py` / `pool_async.py`). Connections close outside
the lock, mirroring `drain()`, so a slow close cannot pin the pool.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool


async def release_idle_async(pool: AsyncConnectionPool[psycopg.AsyncConnection]) -> int:
    """Close every idle connection in an async pool; return the count released."""
    async with pool._lock:
        conns = list(pool._pool)
        pool._pool.clear()
        pool._nconns -= len(conns)
        pool._nconns_min = min(pool._nconns_min, len(pool._pool))
    for conn in conns:
        await pool._close_connection(conn)
    return len(conns)


def release_idle_sync(pool: ConnectionPool[psycopg.Connection]) -> int:
    """Close every idle connection in a sync pool; return the count released."""
    with pool._lock:
        conns = list(pool._pool)
        pool._pool.clear()
        pool._nconns -= len(conns)
        pool._nconns_min = min(pool._nconns_min, len(pool._pool))
    for conn in conns:
        pool._close_connection(conn)
    return len(conns)
