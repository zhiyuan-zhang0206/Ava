"""Contract for the unused marker drop and its reconstructible settle rollback.

The rollback reads `deployment_state.settle_note`, a column a later migration
drops, so the later migration's rollback runs first, in the order a manual
rollback applies them.
"""

from pathlib import Path
from typing import LiteralString, cast

import psycopg


def test_retired_markers_drop_and_rollback(db_conn: psycopg.Connection) -> None:
    directory = Path(__file__).resolve().parents[2] / "migrations"
    name = "20260923T031517_drop-retired-schema-markers"
    up = cast(LiteralString, (directory / f"{name}.sql").read_text())
    down = cast(LiteralString, (directory / f"{name}.down.sql").read_text())
    later = "20261001T055030_drop-retired-deploy-and-watcher-storage"
    later_down = cast(LiteralString, (directory / f"{later}.down.sql").read_text())
    with db_conn.transaction(force_rollback=True):
        assert (
            db_conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                "AND ((table_name='agents_meta' AND column_name='last_compact_at') "
                "OR (table_name='deployment_state' AND column_name='note'))"
            ).fetchall()
            == []
        )
        db_conn.execute(later_down, prepare=False)
        db_conn.execute(down, prepare=False)
        db_conn.execute(
            "UPDATE deployment_state SET settle_hosts=ARRAY['worker'], "
            "settle_note='settling, waiting for: worker', note='obsolete' WHERE id=1"
        )
        db_conn.execute(up, prepare=False)
        assert (
            db_conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                "AND ((table_name='agents_meta' AND column_name='last_compact_at') "
                "OR (table_name='deployment_state' AND column_name='note'))"
            ).fetchall()
            == []
        )
        assert db_conn.execute(
            "SELECT settle_hosts, settle_note FROM deployment_state WHERE id=1"
        ).fetchone() == (["worker"], "settling, waiting for: worker")
        db_conn.execute(down, prepare=False)
        assert db_conn.execute("SELECT note FROM deployment_state WHERE id=1").fetchone() == (
            "settling, waiting for: worker",
        )
