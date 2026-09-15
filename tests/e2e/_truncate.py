"""Deadlock-retried TRUNCATE for the e2e per-test isolation fixture.

Split out of `tests/e2e/conftest.py` (`truncated_db`) so the retry semantics
are unit-testable (`tests/test_e2e_truncate_retry.py`) without importing the
conftest module.
"""

from __future__ import annotations

from typing import Any, LiteralString

import psycopg


def truncate_with_deadlock_retry(conn: psycopg.Connection[Any], statement: LiteralString) -> None:
    """Run one TRUNCATE, retrying `DeadlockDetected` up to 3 attempts total.

    Rollback between attempts; re-raise on the third so a persistent failure is
    never absorbed — the loop only rides out the transient lock-order race.
    Mirrors the non-e2e suite's per-test truncate (tests/conftest.py
    `_clean_state` — keep the two in step).
    """
    for attempt in range(3):
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
            conn.commit()
            return
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise
            conn.rollback()
