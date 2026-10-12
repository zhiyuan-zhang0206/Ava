"""Unit coverage for `meizel.owners.decisions` (Rule 5, single decision owners)."""

from __future__ import annotations

import ast
import pathlib

import pytest

from meizel.owners import decisions


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- owner_bypasses: the postgres-dial single decision owner ----------------

_POSITIVE_DIALS = {
    "module_connect": "import psycopg\npsycopg.connect('dsn')\n",
    "aliased_module_connect": "import psycopg as pg\npg.connect('dsn')\n",
    "from_connect_function": "from psycopg import connect\nconnect()\n",
    "aliased_connect_function": "from psycopg import connect as c\nc()\n",
    "from_async_connection_connect": (
        "from psycopg import AsyncConnection\nAsyncConnection.connect()\n"
    ),
    "qualified_async_connection_connect": "import psycopg\npsycopg.AsyncConnection.connect()\n",
    "connection_pool": "from psycopg_pool import ConnectionPool\nConnectionPool()\n",
    "subscripted_aliased_async_connection_pool": (
        "from psycopg_pool import AsyncConnectionPool as P\nP[int]()\n"
    ),
    "qualified_pool_module_connection_pool": (
        "import psycopg_pool\npsycopg_pool.ConnectionPool()\n"
    ),
}


@pytest.mark.parametrize("source", _POSITIVE_DIALS.values(), ids=_POSITIVE_DIALS.keys())
def test_postgres_dial_forms_are_flagged(source: str) -> None:
    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ())

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_governed_subclass_construction_is_a_dial() -> None:
    """This repo's own pool subclasses (e.g. agent/db/__init__.py's LoggingConnectionPool)
    are governed reach-ins too, not just the psycopg_pool names themselves."""
    source = "from agent.db import LoggingConnectionPool\nLoggingConnectionPool[int]('dsn')\n"

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"))

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_ungoverned_subclass_construction_is_not_a_dial() -> None:
    source = "from somepkg.db import LoggingConnectionPool\nLoggingConnectionPool()\n"

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"))

    assert sites == {}


def test_a_locally_defined_pool_subclass_is_a_dial() -> None:
    source = (
        "from psycopg_pool import AsyncConnectionPool\n"
        "class P(AsyncConnectionPool[int]):\n"
        "    pass\n\n\n"
        "P()\n"
    )

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ())

    assert sites == {"gateway/db.py::postgres-dial": [6]}


_NEGATIVE_DIALS = {
    "sqlite3_connect": "import sqlite3\nsqlite3.connect('file.db')\n",
    "connection_from_a_non_psycopg_module": (
        "from mylib import Connection\nConnection.connect()\n"
    ),
    "pool_method_call": (
        "from psycopg_pool import ConnectionPool\nConnectionPool.check_connection('x')\n"
    ),
    "unbound_redis_attribute_pool": "import redis\nredis.ConnectionPool()\n",
    "ungoverned_third_party_pool": "from redis import ConnectionPool\nConnectionPool()\n",
    "unbound_urllib3_https_pool": "import urllib3\nurllib3.HTTPSConnectionPool('h')\n",
    "pool_timeout_from_import": "from psycopg_pool import PoolTimeout\nPoolTimeout()\n",
    "pool_timeout_qualified": "import psycopg_pool\npsycopg_pool.PoolTimeout()\n",
    "bare_annotation": "from psycopg import Connection\ndef f(conn: Connection) -> None: ...\n",
}


@pytest.mark.parametrize("source", _NEGATIVE_DIALS.values(), ids=_NEGATIVE_DIALS.keys())
def test_non_dial_calls_are_not_flagged(source: str) -> None:
    assert decisions.owner_bypasses(_parse(source), "gateway/db.py", ()) == {}


def test_the_owner_module_itself_is_exempt() -> None:
    tree = _parse("import psycopg\npsycopg.connect('dsn')\n")

    assert decisions.owner_bypasses(tree, "base/db/connections.py", ()) == {}


# --- allowlist_errors / missing_allowlist_errors -----------------------------


def test_allowlist_entry_is_stale_once_the_module_stops_bypassing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = decisions.Decision(
        owners=frozenset({"base/owner.py"}),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(decisions, "DECISIONS", {"fake-decision": decision})

    errors = decisions.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py", ())

    assert len(errors) == 1
    lineno, message = errors[0]
    assert lineno == 1
    assert "stale fake-decision allowlist entry" in message
    assert "remove it from DECISIONS" in message


def test_allowlist_entry_is_not_stale_while_it_still_bypasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = decisions.Decision(
        owners=frozenset({"base/owner.py"}),
        find=lambda _tree, _roots: [3],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(decisions, "DECISIONS", {"fake-decision": decision})

    assert decisions.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py", ()) == []


def test_allowlist_errors_ignore_modules_outside_the_allowed_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = decisions.Decision(
        owners=frozenset(), find=lambda _tree, _roots: [], fix="use the owner", allowed={}
    )
    monkeypatch.setattr(decisions, "DECISIONS", {"fake-decision": decision})

    assert decisions.allowlist_errors(_parse("x = 1\n"), "gateway/other.py", ()) == []


def test_missing_allowlist_errors_flags_a_deleted_allowed_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = decisions.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/gone.py": "historical exemption"},
    )
    monkeypatch.setattr(decisions, "DECISIONS", {"fake-decision": decision})

    errors = decisions.missing_allowlist_errors(tmp_path)

    assert len(errors) == 1
    assert "gateway/gone.py:1: stale fake-decision allowlist entry" in errors[0]
    assert "the module no longer exists" in errors[0]


def test_missing_allowlist_errors_is_clean_for_an_existing_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "gateway/present.py", "x = 1\n")
    decision = decisions.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/present.py": "historical exemption"},
    )
    monkeypatch.setattr(decisions, "DECISIONS", {"fake-decision": decision})

    assert decisions.missing_allowlist_errors(tmp_path) == []
