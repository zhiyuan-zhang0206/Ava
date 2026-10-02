"""A small connection pool for tests that call ops entry points which take a pool."""

from __future__ import annotations

from typing import cast

from psycopg_pool import ConnectionPool

from base.config import settings


def make_test_pool() -> ConnectionPool:
    """Return a concretely typed pool for helpers that open their own pool."""
    return cast(
        ConnectionPool,
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2),
    )
