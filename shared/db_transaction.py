"""Explicit database transaction postures."""

import contextlib
from collections.abc import Generator

import psycopg

from shared.db import connect


@contextlib.contextmanager
def write_transaction() -> Generator[psycopg.Connection, None, None]:
    """Open one transaction explicitly allowed to write.

    PgBouncer can assign a client a backend whose session state another client
    changed. Compensating operations declare their transaction writable before
    DML, pinning that backend until the context commits or rolls back.
    """
    with connect() as conn:
        conn.execute("SET TRANSACTION READ WRITE")
        yield conn
