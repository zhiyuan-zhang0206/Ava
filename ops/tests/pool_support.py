from __future__ import annotations

from typing import cast

from psycopg_pool import ConnectionPool

from base.config import settings


def make_test_pool() -> ConnectionPool:
    return cast(
        ConnectionPool,
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2),
    )
