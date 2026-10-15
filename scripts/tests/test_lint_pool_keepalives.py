from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint import pool_keepalives as _lint


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(src)


def test_pool_with_only_prepare_threshold_is_flagged():
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
    src = (
        "async with LoggingConnectionPool[psycopg.AsyncConnection](\n"
        "    url,\n"
        '    kwargs={"autocommit": True},\n'
        ") as p:\n"
        "    pass\n"
    )
    assert len(_violations(src)) == 1


def test_constant_reached_by_module_attribute_is_clean():
    src = "pool = ConnectionPool(url, kwargs={**base.db.PG_KEEPALIVE_KWARGS})\n"
    assert _violations(src) == []


def test_annotation_is_not_a_construction():
    src = "_db_pool: ConnectionPool | None = None\n"
    assert _violations(src) == []


def test_check_connection_classmethod_is_not_a_construction():
    src = "await AsyncConnectionPool.check_connection(conn)\n"
    assert _violations(src) == []


def test_non_literal_kwargs_dict_is_flagged():
    src = "pool = ConnectionPool(url, kwargs=some_dict)\n"
    assert len(_violations(src)) == 1


def test_repo_is_clean() -> None:
    assert _lint.main([]) == 0


def test_default_scope_is_what_rule_5_does_not_see() -> None:
    from meizel.owners.decisions import DECISIONS

    root = Path(__file__).resolve().parents[2]
    scanned = {path.relative_to(root).as_posix() for path in _lint.default_targets()}
    assert scanned == {"scripts", "meizel", *DECISIONS["postgres-dial"].allowed}


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert _lint.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert _lint.main([str(good), str(missing)]) == 1


def test_directory_with_dangling_symlink_member_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert _lint.main([str(tmp_path)]) == 0
    (tmp_path / "viol.py").write_text(
        "pool = ConnectionPool(url, min_size=1, max_size=2, open=True)\n", encoding="utf-8"
    )
    assert _lint.main([str(tmp_path)]) == 1
    assert "passes no `kwargs=`" in capsys.readouterr().out
