"""Validate inspector liveness at the database read boundary."""

import pytest


@pytest.mark.parametrize("state", ["online", "offline", "unknown"])
def test_inspector_parses_liveness_at_db_boundary(state: str) -> None:
    from unittest.mock import MagicMock

    from base.agents import LivenessState
    from gateway.inspect._live import db_rows_blocking

    pool = MagicMock()
    cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [
        ({}, None, "machine", "idling", None, None, None, None, None, state, None, None, None),
        (False,),
    ]
    result = db_rows_blocking(pool, 1)
    assert result.liveness_state is LivenessState(state)


@pytest.mark.parametrize("state", [None, "unrecognized"])
def test_inspector_rejects_missing_or_unknown_liveness(state: str | None) -> None:
    from unittest.mock import MagicMock

    from gateway.inspect._live import db_rows_blocking

    pool = MagicMock()
    cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [
        ({}, None, "machine", "idling", None, None, None, None, None, state, None, None, None),
        (False,),
    ]
    with pytest.raises(ValueError):
        db_rows_blocking(pool, 1)
