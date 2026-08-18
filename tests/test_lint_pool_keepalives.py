"""`scripts/lint_pool_keepalives.py` — no psycopg pool without TCP keepalives.

The invariant is easy to restate and easy to forget: a pool whose connections
carry no keepalives hands out sockets that may have died during a host sleep, and
a query on one has no application-level bound. Three sync pools each wrote
`kwargs={"prepare_threshold": None}` and stopped there, which is what this lint now
makes impossible to reintroduce.

These cases pin both shapes a real site takes (the sync construction that should
have called `shared.db.pool()`, and the generic-subscripted async pool that
legitimately unpacks the constant itself), plus the things that must NOT be
flagged: annotations, `check_connection`, and the constant reached through a
module attribute.
"""

from __future__ import annotations

import importlib

_lint = importlib.import_module("scripts.lint_pool_keepalives")


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(src)


def test_pool_with_only_prepare_threshold_is_flagged():
    # Verbatim shape of the defect: the three sites all looked like this.
    src = (
        "pool = ConnectionPool(\n"
        "    settings.data_plane.db_url,\n"
        "    min_size=1,\n"
        "    max_size=8,\n"
        "    open=True,\n"
        '    kwargs={"prepare_threshold": None},\n'
        ")\n"
    )
    assert len(_violations(src)) == 1


def test_pool_with_no_kwargs_at_all_is_flagged():
    src = "pool = ConnectionPool(url, min_size=1, max_size=2, open=True)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `kwargs=`" in violations[0][1]


def test_pool_unpacking_the_constant_is_clean():
    src = (
        "pool = ConnectionPool(\n"
        "    url,\n"
        '    kwargs={"prepare_threshold": None, **PG_KEEPALIVE_KWARGS},\n'
        ")\n"
    )
    assert _violations(src) == []


def test_async_pool_with_generic_subscript_is_seen_through():
    # agent/loop.py's real shape — the class is subscripted
    # with a type argument, so a naive check on `node.func` being a Name misses it.
    bad = (
        "async with AsyncConnectionPool[psycopg.AsyncConnection](\n"
        "    url,\n"
        '    kwargs={"autocommit": True},\n'
        ") as p:\n"
        "    pass\n"
    )
    good = (
        "async with AsyncConnectionPool[psycopg.AsyncConnection](\n"
        "    url,\n"
        '    kwargs={"autocommit": True, **PG_KEEPALIVE_KWARGS},\n'
        ") as p:\n"
        "    pass\n"
    )
    assert len(_violations(bad)) == 1
    assert _violations(good) == []


def test_subclass_of_connection_pool_is_covered():
    # agent/loop.py builds a LoggingConnectionPool, not a ConnectionPool — the
    # suffix match is what keeps a subclass from being a free pass.
    src = (
        "async with LoggingConnectionPool[psycopg.AsyncConnection](\n"
        "    url,\n"
        '    kwargs={"autocommit": True},\n'
        ") as p:\n"
        "    pass\n"
    )
    assert len(_violations(src)) == 1


def test_constant_reached_by_module_attribute_is_clean():
    src = "pool = ConnectionPool(url, kwargs={**shared.db.PG_KEEPALIVE_KWARGS})\n"
    assert _violations(src) == []


def test_annotation_is_not_a_construction():
    src = "_db_pool: ConnectionPool | None = None\n"
    assert _violations(src) == []


def test_check_connection_classmethod_is_not_a_construction():
    src = "await AsyncConnectionPool.check_connection(conn)\n"
    assert _violations(src) == []


def test_non_literal_kwargs_dict_is_flagged():
    # The lint reads source; a name it cannot see through would be an
    # unverifiable pass, so it is treated as a miss rather than trusted.
    src = "pool = ConnectionPool(url, kwargs=some_dict)\n"
    assert len(_violations(src)) == 1


def test_repo_is_clean() -> None:
    """The gate is only as good as its verdict on the tree it guards: every pool
    under the scanned directories must already pass."""
    assert _lint.main([]) == 0
