"""Native readiness never authorizes changes to a foreign data-plane listener."""

import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock
from urllib.parse import urlsplit

import pytest
import redis
from redis.asyncio import Redis as AsyncRedis

from base.cluster import ownership
from base.cluster import postgres as pg
from base.cluster.dataplane import pooler as base_pooler
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer as pooler
from tests._containers import redis_server
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_UNUSED_ADMIN = "unused-admin-credential"


@pytest.fixture
def pg_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PostgreSQL data directory inside a private home.

    Custody keeps its receipt in `<data>/../run`, as a home's `pgdata` sits beside its `run`.
    A data directory directly under the session's shared base temp directory would hand every
    other test the receipt this one writes.
    """
    data = tmp_path_factory.mktemp("home") / "pgdata"
    data.mkdir()
    return data


def test_foreign_redis_keeps_acl_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign Redis that even accepts this home's admin password is not ours:
    native ownership refuses before any ACL or config effect."""
    config = tmp_path / "redis.conf"
    config.write_text("original config\n")
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(instance, "_redis_dial_host", lambda: "127.0.0.1")
    monkeypatch.setattr(instance, "_redis_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with redis_server() as url:
        port = urlsplit(url).port
        assert port is not None
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            client.config_set("requirepass", "admin")  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            before = client.execute_command("ACL", "LIST")  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            assert instance.start_redis(port, "admin", "runtime", "", "unowned") == 1
            assert client.execute_command("ACL", "LIST") == before  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
    assert config.read_text() == "original config\n"


def test_foreign_postgres_listener_refuses_before_hba_or_initdb(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    monkeypatch.setattr(instance, "_pg_data_dir", lambda: pg_data)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(instance, "_ensure_pg_data", lambda: pytest.fail("must not initialize"))
    with pytest.raises(RuntimeError, match="no owned data-plane process"):
        instance._start_pg(15433, "", retained_children=retained_children)
    assert not (pg_data / "pg_hba.conf").exists()


def test_foreign_pooler_listener_refuses_before_config_or_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base_pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pooler, "pgbouncer_bin", lambda: __file__)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(pooler, "_write_config", lambda **_kw: pytest.fail("must not write"))  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with pytest.raises(RuntimeError, match="no owned data-plane process"):
        pooler.ensure_pgbouncer(
            pg_port=15433,
            listen_port=16433,
            db_name="ava",
            cluster_secret="",
            userlist=b"",
            admin_password=_UNUSED_ADMIN,
        )


def test_pooler_birth_change_prevents_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base_pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pooler, "pgbouncer_bin", lambda: __file__)
    monkeypatch.setattr(
        ownership,
        "pooler",
        lambda *_a: SimpleNamespace(pid=123, live=lambda: False),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(ownership, "require_listener", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(pooler, "_write_config", lambda **_kw: False)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(base_pooler, "pgbouncer_public_listener_reachable", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(
        pooler,
        "psutil",
        SimpleNamespace(
            Process=lambda _pid: SimpleNamespace(
                send_signal=lambda _sig: pytest.fail("replacement signalled")
            )
        ),
    )
    with pytest.raises(RuntimeError, match="identity changed before reload"):
        pooler.ensure_pgbouncer(
            pg_port=15433,
            listen_port=16433,
            db_name="ava",
            cluster_secret="",
            userlist=b"",
            admin_password=_UNUSED_ADMIN,
        )


def test_redis_maintenance_reconnect_cannot_shutdown_another_connection(
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from base.config import settings
    from cli.commands.data_plane import maintenance_stop as plane

    monkeypatch.setattr(plane, "capture_postgres", lambda: None)
    monkeypatch.setattr(plane, "_capture_pooler", lambda: None)
    monkeypatch.setattr(plane, "_require_no_unrecorded", lambda _captured: None)  # pyright: ignore[reportUnknownArgumentType] — test double
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    capture = ownership.RedisConnectionCustody.capture

    async def disconnect_after_capture(
        custody: ownership.RedisConnectionCustody,
        client: AsyncRedis,
        *,
        port: int,
        data_dir: Path,
        deadline: float,
    ) -> ownership.OwnedProcess | None:
        result = await capture(custody, client, port=port, data_dir=data_dir, deadline=deadline)
        assert client.connection is not None
        await client.connection.disconnect()  # pyright: ignore[reportUnknownMemberType] — redis stubs
        return result

    monkeypatch.setattr(ownership.RedisConnectionCustody, "capture", disconnect_after_capture)
    with redis_server() as url:
        monkeypatch.setattr(settings.data_plane, "redis_url", url)
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            directory = Path(str(client.config_get("dir")["dir"]))  # pyright: ignore[reportUnknownMemberType] — redis stubs
            monkeypatch.setattr(ownership, "redis_data_dir", lambda: directory)
            with pytest.raises(RuntimeError, match="connection changed"):
                plane.stop(3, producer=operator_pipeline)
            assert client.ping(), "the server must survive lost connection custody"  # pyright: ignore[reportUnknownMemberType] — redis stubs


def _private_pg_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    from base.config import settings
    from tests._containers import _free_port

    home, data = tmp_path / "home", tmp_path / "home/pg"
    home.mkdir()
    port = _free_port()
    monkeypatch.setenv("AVA_HOME", str(home))
    monkeypatch.setattr(settings.data_plane, "db_url", f"postgresql://test@127.0.0.1:{port}/test")
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    return data, port


def _assert_pg_birth(data: Path, port: int) -> tuple[OwnedProcess, bytes]:
    owner = ownership.require_postgres(data, port)
    assert owner is not None
    receipt = pg._read(data)
    assert receipt is not None
    assert (receipt.state, receipt.boot_id) == ("ready", pg._boot())
    return owner, pg.receipt_path(data).read_bytes()


def _forbid_new_pg_admission(patch: pytest.MonkeyPatch) -> None:
    """A retained birth must neither prepare a pending receipt nor launch a child."""
    write = pg.write_text_atomic

    def retained_receipt(path: Path, text: str, *, sync_parent: bool = False) -> None:
        receipt = pg.Receipt.model_validate_json(text)
        assert receipt.state != "pending", "retained startup prepared a new admission"
        write(path, text, sync_parent=sync_parent)

    def retained_process(argv: list[str], **_kwargs: object) -> object:
        assert Path(argv[0]).name != "postgres", "birth replacement"
        return DEFAULT

    patch.setattr(pg, "write_text_atomic", retained_receipt)
    patch.setattr(subprocess, "Popen", Mock(wraps=subprocess.Popen, side_effect=retained_process))


def _assert_warm_pg_repeat(
    data: Path,
    port: int,
    first: OwnedProcess,
    persisted: bytes,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    assert instance._start_pg(port, "", retained_children=retained_children) == 0
    repeated = ownership.postgres(data)
    assert repeated is not None and first.same_birth(repeated)
    assert pg.receipt_path(data).read_bytes() == persisted


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_owned_postgres_resume_and_fast_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """The ordinary producer supplies all custody; no test-only receipt adoption."""
    import psycopg

    from cli.commands.data_plane import maintenance_stop as plane

    data, port = _private_pg_configuration(tmp_path, monkeypatch)
    try:
        assert instance._start_pg(port, "", retained_children=retained_children) == 0
        first, persisted = _assert_pg_birth(data, port)
        with psycopg.connect(instance.pg_admin_url(port), autocommit=True) as conn:
            ownership.require_postgres_connection(conn, data)
            row = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
            assert row is not None
            system_id = row[0]
            _assert_warm_pg_repeat(data, port, first, persisted, retained_children)
            assert plane.stop(
                10, retained_children=retained_children, producer=operator_pipeline
            ) == ["postgres"]
            assert not first.live() and ownership.postgres(data) is None
            assert pg.receipt_path(data).read_bytes() == persisted
            with pytest.raises(psycopg.OperationalError):
                conn.execute("SELECT 1")
        assert plane.stop(2, retained_children=retained_children, producer=operator_pipeline) == []
        assert instance._start_pg(port, "", retained_children=retained_children) == 0
        second, _receipt = _assert_pg_birth(data, port)
        assert not first.same_birth(second)
        with psycopg.connect(instance.pg_admin_url(port), autocommit=True) as conn:
            ownership.require_postgres_connection(conn, data)
            assert conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone() == (
                system_id,
            )
        with pytest.raises(RuntimeError, match="replacement"):
            pg.stop(data, expected=first, retained_children=retained_children)
        assert second.live()
    finally:
        pg.stop(data, timeout=10, retained_children=retained_children)
    assert ownership.postgres(data) is None
    ownership.require_listener(None, port, required=False)


def test_pg_native_signal_failure_is_not_reported_as_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import asyncio

    from cli.commands.data_plane import maintenance_stop as plane
    from tests._containers import _free_port

    owner = OwnedProcess(123, 100.0, 456)
    seen: list[OwnedProcess | None] = []

    def fail(
        _data: Path,
        *,
        expected: OwnedProcess | None,
        timeout: float,
        immediate_wait: float,
        kill_wait: float,
        retained_children: list[subprocess.Popen[bytes]] | None,
    ) -> None:
        assert timeout > 0 and immediate_wait > 0 and kill_wait > 0
        seen.append(expected)
        raise RuntimeError("native custody lost")

    monkeypatch.setattr(plane.owned_postgres, "stop", fail)
    client = AsyncRedis(port=_free_port())
    with pytest.raises(RuntimeError, match="native custody lost"):
        asyncio.run(
            plane._request_stop(
                "postgres",
                owner,
                client,
                plane.deadline_after(3),
                save=True,
                producer=operator_pipeline,
            )
        )
    assert seen == [owner]


def test_pgdata_symlink_cannot_adopt_another_homes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    foreign = tmp_path / "foreign/pg"
    foreign.mkdir(parents=True)
    hba = foreign / "pg_hba.conf"
    hba.write_text("foreign authority\n")
    local = tmp_path / "local/pg"
    local.parent.mkdir()
    local.symlink_to(foreign, target_is_directory=True)
    monkeypatch.setattr(instance, "_pg_data_dir", lambda: local)
    monkeypatch.setattr(
        pg, "regular_bytes", Mock(side_effect=AssertionError("foreign receipt read"))
    )
    monkeypatch.setattr(
        instance, "_ensure_pg_data", Mock(side_effect=AssertionError("initdb effect"))
    )
    with pytest.raises(RuntimeError, match="symlink"):
        instance._start_pg(15433, "", retained_children=retained_children)
    assert hba.read_text() == "foreign authority\n"


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_stopped_postmaster_cannot_pass_warm_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    data, port = _private_pg_configuration(tmp_path, monkeypatch)
    try:
        assert instance._start_pg(port, "", retained_children=retained_children) == 0
        owner, before = _assert_pg_birth(data, port)
        assert owner.send_signal(signal.SIGSTOP)
        try:
            with monkeypatch.context() as context:
                context.setattr(instance, "_PG_START_TIMEOUT_S", 0.25)
                context.setattr(instance, "_PG_PROBE_TIMEOUT_S", 0.1)
                _forbid_new_pg_admission(context)
                with pytest.raises(RuntimeError, match="did not become ready"):
                    instance._start_pg(port, "", retained_children=retained_children)
            assert owner.live() and pg.receipt_path(data).read_bytes() == before
        finally:
            owner.send_signal(signal.SIGCONT)
        _assert_warm_pg_repeat(data, port, owner, before, retained_children)
    finally:
        pg.stop(data, timeout=10, retained_children=retained_children)


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_interrupted_admission_completes_same_postmaster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    data, port = _private_pg_configuration(tmp_path, monkeypatch)
    protocol = instance._pg_running

    def interrupted(port: int, host: str) -> bool:
        if protocol(port, host):
            raise KeyboardInterrupt("interrupted after protocol success before admission")
        return False

    try:
        with monkeypatch.context() as context:
            context.setattr(instance, "_pg_running", interrupted)
            with pytest.raises(KeyboardInterrupt, match="before admission"):
                instance._start_pg(port, "", retained_children=retained_children)
        captured = pg._read(data)
        assert captured is not None and captured.state == "captured"
        owner = captured.process()
        with monkeypatch.context() as context:
            _forbid_new_pg_admission(context)
            assert instance._start_pg(port, "", retained_children=retained_children) == 0
        admitted, persisted = _assert_pg_birth(data, port)
        assert owner.same_birth(admitted)
        _assert_warm_pg_repeat(data, port, owner, persisted, retained_children)
    finally:
        pg.stop(data, timeout=10, retained_children=retained_children)


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []
