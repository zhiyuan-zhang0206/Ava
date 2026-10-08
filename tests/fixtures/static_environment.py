"""Process-level environment for explicitly owned file/AST contracts.

The membership below owns both CI's static run and its native exclusions.
It declares execution requirements; it grants no lint or isolation exemption.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import pytest

STATIC_TEST_PATHS = (
    "scripts/audit/tests",
    "scripts/content_lint/tests",
    "scripts/lint/tests",
    "scripts/structure/tests",
    "tests/fixtures/tests/test_static_environment.py",
    "tests/fixtures/tests/test_static_environment_selection.py",
)


class TestEnvironment(StrEnum):
    NATIVE = "native"
    STATIC = "static"


def static_mode(config: pytest.Config) -> bool:
    return TestEnvironment(config.getoption("test_environment")) is TestEnvironment.STATIC


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Ava test environment")
    group.addoption(
        "--test-environment",
        choices=tuple(TestEnvironment),
        default=TestEnvironment.NATIVE,
        help="Use the default native data plane or the isolated static-contract process.",
    )
    group.addoption(
        "--omit-static-tests",
        action="store_true",
        help="Exclude the static lane already verified by required CI structure checks.",
    )


def _owned_path(config: pytest.Config, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(config.rootpath.resolve())
    except ValueError:
        return False
    return any(
        relative == Path(owned)
        or ((config.rootpath / owned).is_dir() and relative.is_relative_to(owned))
        for owned in STATIC_TEST_PATHS
    )


def _argument_path(config: pytest.Config, arg: str) -> Path:
    path = Path(arg.split("::", 1)[0])
    return path if path.is_absolute() else config.invocation_params.dir / path


def _refuse_data_plane(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("Static tests cannot use Postgres or Redis; use the native test environment.")


def _refuse_native_data_plane(patch: pytest.MonkeyPatch) -> None:
    import psycopg
    import redis
    import redis.asyncio
    from redis.asyncio.connection import AbstractConnection as AsyncRedisConnection
    from redis.connection import AbstractConnection as RedisConnection

    from tests import _containers
    from tests.fixtures.guards import _stub_everywhere

    for module, name in [
        (psycopg, "connect"),
        (_containers, "postgres"),
        (_containers, "redis_server"),
    ]:
        _stub_everywhere(patch, module, name, _refuse_data_plane)
    for connection in (psycopg.Connection, psycopg.AsyncConnection):
        for name in ("connect", "cursor", "execute"):
            patch.setattr(connection, name, _refuse_data_plane)
    for cursor in (psycopg.Cursor, psycopg.AsyncCursor):
        for name in ("execute", "executemany"):
            patch.setattr(cursor, name, _refuse_data_plane)
    for client in (redis.Redis, redis.asyncio.Redis):
        patch.setattr(client, "execute_command", _refuse_data_plane)
    for transport in (RedisConnection, AsyncRedisConnection):
        for name in ("connect", "send_packed_command"):
            patch.setattr(transport, name, _refuse_data_plane)


def _select_static_paths(config: pytest.Config) -> None:
    if config.args_source is not pytest.Config.ArgsSource.ARGS:
        config.args[:] = [str(config.rootpath / path) for path in STATIC_TEST_PATHS]
    outside = [arg for arg in config.args if not _owned_path(config, _argument_path(config, arg))]
    if outside:
        raise pytest.UsageError(f"Static processes accept only owned static test paths: {outside}")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if not static_mode(config) and not config.getoption("omit_static_tests"):
        return
    missing = [
        path
        for path in STATIC_TEST_PATHS
        if not (config.rootpath / path).is_file() and not (config.rootpath / path).is_dir()
    ]
    if missing:
        raise pytest.UsageError(f"Static test ownership names missing paths: {missing}")
    if not static_mode(config):
        config.args[:] = [
            arg for arg in config.args if not _owned_path(config, _argument_path(config, arg))
        ]
        return
    if config.getoption("omit_static_tests"):
        raise pytest.UsageError("The static environment cannot omit its own tests.")
    _select_static_paths(config)
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    _refuse_native_data_plane(patch)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if config.getoption("omit_static_tests") and _owned_path(config, collection_path):
        return True
    return None


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if static_mode(config):
        outside = [item.nodeid for item in items if not _owned_path(config, item.path)]
        if outside:
            raise pytest.UsageError(f"Non-static tests reached the static process: {outside}")
