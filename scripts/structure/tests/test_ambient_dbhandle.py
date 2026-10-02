"""The ambient-db rule (scripts/structure/ambient_state/dbhandle.py): a package that holds a
`Database` handle dials nothing from the live settings."""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from scripts.structure import ambient_state
from scripts.structure.ambient_state import allowlist as allow

_PACKAGE = "services/thing"
_ROOT = f"{_PACKAGE}/daemon.py"


@pytest.fixture(autouse=True)
def _governed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared structure-test isolation blanks the real list; govern one package here."""
    monkeypatch.setattr(allow, "DB_HANDLE_PACKAGES", {_PACKAGE: frozenset({_ROOT})})


def _sites(source: str, rel: str = f"{_PACKAGE}/core.py") -> dict[str, int]:
    tree = ast.parse(textwrap.dedent(source))
    measured = ambient_state.measure(tree, rel, pathlib.Path("/nonexistent"))
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


@pytest.mark.parametrize(
    ("imports", "call", "expected"),
    [
        ("from base.db import connect", "connect()", "connect"),
        ("from base.db import pool as _pool", "_pool()", "pool"),
        ("from base.db.connections import async_pool", "async_pool(X)", "async_pool"),
        ("import base.db", "base.db.pool()", "pool"),
        ("import base.db as dbm", "dbm.connect(direct=True)", "connect"),
        ("from base import db", "db.direct_db_url()", "direct_db_url"),
        ("from base.db import write_transaction", "write_transaction()", "write_transaction"),
        ("from base.db import Database", "Database.from_settings()", "Database.from_settings"),
    ],
)
def test_ambient_dials_in_a_governed_package_are_sites(
    imports: str, call: str, expected: str
) -> None:
    source = f"{imports}\n\n\ndef f():\n    return {call}\n"
    assert _sites(source) == {f"ambient-db:{expected}": 1}


def test_a_write_transaction_with_a_pool_is_not_ambient() -> None:
    source = """
        from base.db.transaction import write_transaction

        def f(pool):
            with write_transaction(pool) as c:
                pass
            with write_transaction(pool=pool) as c:
                pass
    """
    assert _sites(source) == {}


def test_calls_on_a_handle_are_not_sites() -> None:
    source = """
        def run(db):
            conn = db.connect()
            pool = db.pool()
            with db.write_transaction() as c:
                pass
            return conn, pool, c
    """
    assert _sites(source) == {}


def test_the_root_may_build_the_handle_but_not_dial_ambiently() -> None:
    source = """
        from base.db import Database, connect

        def f():
            Database.from_settings()
            return connect()
    """
    assert _sites(source, _ROOT) == {"ambient-db:connect": 1}


@pytest.mark.parametrize("rel", ["services/other/core.py", f"{_PACKAGE}/tests/test_core.py"])
def test_other_modules_and_tests_are_outside_the_rule(rel: str) -> None:
    source = "from base.db import connect\n\n\ndef f():\n    return connect()\n"
    assert _sites(source, rel) == {}


def test_a_listed_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "DB_HANDLE_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "DB_HANDLE" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
