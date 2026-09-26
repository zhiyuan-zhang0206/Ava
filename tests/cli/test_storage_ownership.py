"""Native readiness never authorizes changes to a foreign data-plane listener."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import redis
from redis.asyncio import Redis as AsyncRedis

from cli.commands import _cluster_instance as instance
from cli.commands import _pgbouncer as pooler
from shared.cluster import ownership
from shared.cluster import postgres as pg
from shared.native_process.ownership import OwnedProcess
from tests._containers import redis_server


def test_foreign_no_auth_redis_keeps_acl_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "redis.conf"
    config.write_text("original config\n")
    monkeypatch.setattr(instance, "_redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(instance, "_redis_dial_host", lambda: "127.0.0.1")
    monkeypatch.setattr(instance, "_redis_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with redis_server() as url:
        port = urlsplit(url).port
        assert port is not None
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            before = client.execute_command("ACL", "LIST")  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            assert instance._start_redis(port, "", "", "", "unowned") == 1
            assert client.execute_command("ACL", "LIST") == before  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
    assert config.read_text() == "original config\n"


def test_foreign_postgres_listener_refuses_before_hba_or_initdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance, "_pg_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(instance, "_ensure_pg_data", lambda: pytest.fail("must not initialize"))
    with pytest.raises(RuntimeError, match="no owned data-plane process"):
        instance._start_pg(15433, "")
    assert not (tmp_path / "pg_hba.conf").exists()


def test_foreign_pooler_listener_refuses_before_config_or_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pooler, "pgbouncer_bin", lambda: __file__)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(pooler, "_write_config", lambda **_kw: pytest.fail("must not write"))  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with pytest.raises(RuntimeError, match="no owned data-plane process"):
        pooler.ensure_pgbouncer(
            pg_port=15433,
            listen_port=16433,
            db_name="ava",
            role="ava",
            cluster_secret="",
            runner_password="",
        )


def test_pooler_birth_change_prevents_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pooler, "pgbouncer_bin", lambda: __file__)
    monkeypatch.setattr(
        ownership,
        "pooler",
        lambda *_a: SimpleNamespace(pid=123, live=lambda: False),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(ownership, "require_listener", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(pooler, "_write_config", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(pooler, "pgbouncer_public_listener_reachable", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
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
            role="ava",
            cluster_secret="",
            runner_password="",
        )


def test_unrelated_listener_is_not_owned_by_a_valid_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = SimpleNamespace(pid=123, live=lambda: True)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123, 456])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(ownership, "capture_tree", lambda _owner: [owner])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with pytest.raises(RuntimeError, match="does not belong"):
        ownership.require_listener(cast("ownership.OwnedProcess", owner), 15433)


def test_postmaster_pidfile_cannot_supply_missing_native_receipt(tmp_path: Path) -> None:
    (tmp_path / "postmaster.pid").write_text(f"123\n{tmp_path}\n100\n")
    with pytest.raises(RuntimeError, match="no native launch receipt"):
        ownership.postgres(tmp_path)


def test_redis_maintenance_reconnect_cannot_shutdown_another_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import _maintenance_data_plane as plane
    from shared.config import settings

    monkeypatch.setattr(plane, "_capture_postgres", lambda: None)
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
            monkeypatch.setattr(plane.instance, "_redis_data_dir", lambda: directory)
            with pytest.raises(RuntimeError, match="connection changed"):
                plane.stop(3)
            assert client.ping(), "the server must survive lost connection custody"  # pyright: ignore[reportUnknownMemberType] — redis stubs


def _pg_receipt(data: Path, *, state: str = "captured") -> pg.Receipt:
    if sys.platform == "win32":
        pytest.skip("owned PostgreSQL is POSIX-only")
    info = data.stat()
    values = {
        "data": str(data.resolve()),
        "directory": [info.st_dev, info.st_ino],
        "port": 15433,
        "platform": sys.platform,
        "boot_id": pg._boot(),
        "state": state,
        "owner": None
        if state in {"pending", "not-started"}
        else {
            "pid": 123,
            "create_time": 100.0,
            "starttime": 456,
        },
    }
    import json

    return pg.Receipt.model_validate_json(json.dumps(values))


def test_pending_postgres_is_never_recovered_from_a_pidfile(tmp_path: Path) -> None:
    pg._write(tmp_path, _pg_receipt(tmp_path, state="pending"))
    with pytest.raises(RuntimeError, match="unresolved"):
        pg.observe(tmp_path)


def test_postgres_birth_uses_ticks_despite_wall_clock_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _pg_receipt(tmp_path)
    pg._write(tmp_path, receipt)
    (tmp_path / "postmaster.pid").write_text(f"123\n{tmp_path}\n987654321\n15433\n")
    process = SimpleNamespace(
        cmdline=lambda: ["postgres", "-D", str(tmp_path)],
        name=lambda: "postgres",
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(pg.psutil, "Process", Mock(return_value=process))
    monkeypatch.setattr(
        pg.OwnedProcess, "capture", Mock(return_value=OwnedProcess(123, 9000.0, 456))
    )
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=True))
    monkeypatch.setattr(pg.os, "getsid", Mock(return_value=123))
    assert pg.observe(tmp_path) == receipt.process()
    monkeypatch.setattr(
        pg.OwnedProcess, "capture", Mock(return_value=OwnedProcess(123, 100.0, 457))
    )
    with pytest.raises(RuntimeError, match="captured PostgreSQL"):
        pg.observe(tmp_path)


@pytest.mark.parametrize("change", ["directory", "pidfile", "pid", "port"])
def test_postgres_rejects_changed_file_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    receipt = _pg_receipt(tmp_path)
    record = tmp_path / "postmaster.pid"
    record.write_text(f"123\n{tmp_path}\n100\n15433\n")
    inode, header = pg._pidfile(tmp_path, receipt)
    ready = receipt.model_copy(update={"state": "ready", "pidfile": inode, "header": header})
    if change == "pidfile":
        record.rename(tmp_path / "original.pid")
        record.write_text("\n".join(header) + "\n")
    elif change == "directory":
        record.write_text(f"123\n{tmp_path / 'foreign'}\n100\n15433\n")
    elif change == "pid":
        record.write_text(f"456\n{tmp_path}\n100\n15433\n")
    else:
        record.write_text(f"123\n{tmp_path}\n100\n15434\n")
    with pytest.raises(RuntimeError, match="pidfile"):
        pg._pidfile(tmp_path, ready)


def test_prior_boot_never_donates_reused_pid_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _pg_receipt(tmp_path).model_copy(update={"boot_id": "prior-boot"})
    pg._write(tmp_path, receipt)
    monkeypatch.setattr(
        pg.OwnedProcess, "live", Mock(side_effect=AssertionError("old boot PID observed"))
    )
    monkeypatch.setattr(pg, "_require_closed", Mock(return_value=None))
    assert pg.observe(tmp_path) is None


@pytest.mark.parametrize("name", ["postgres", "archive-command"])
def test_dead_postmaster_with_surviving_worker_refuses_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    pg._write(tmp_path, _pg_receipt(tmp_path))
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=False))
    worker = SimpleNamespace(
        info={"pid": 124},
        name=lambda: name,
        pid=124,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: "running",
        cwd=lambda: str(tmp_path),
        cmdline=lambda: ["postgres: checkpoint"],
    )
    monkeypatch.setattr(pg.psutil, "process_iter", Mock(return_value=[worker]))
    monkeypatch.setattr(pg.os, "getsid", Mock(return_value=123))
    with pytest.raises(RuntimeError, match="descendants"):
        pg.observe(tmp_path)


def test_closed_postgres_with_unknown_scan_refuses_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg._write(tmp_path, _pg_receipt(tmp_path))
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=False))

    def unknown(_data: Path, _receipt: pg.Receipt | None) -> None:
        raise PermissionError("native metadata unavailable")

    monkeypatch.setattr(pg, "_require_closed", unknown)
    with pytest.raises(PermissionError):
        pg.observe(tmp_path)


def test_failed_pg_exec_is_not_started_but_ambiguous_spawn_stays_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _pg_receipt(tmp_path, state="pending")
    with pytest.raises(FileNotFoundError):
        pg._spawn(tmp_path, pending, [str(tmp_path / "missing-postgres")], {})
    receipt = pg._read(tmp_path)
    assert receipt is not None and receipt.state == "not-started"

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pg.subprocess, "Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pg._spawn(tmp_path, pending, ["postgres"], {})
    receipt = pg._read(tmp_path)
    assert receipt is not None and receipt.state == "pending"


def test_real_cancellation_after_pg_spawn_retains_native_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", actual(argv, **kwargs))
        children.append(child)
        os.kill(os.getpid(), signal.SIGINT)
        return child

    monkeypatch.setattr(pg.subprocess, "Popen", spawn)
    try:
        with pytest.raises(KeyboardInterrupt, match="admission interrupted"):
            pg._spawn(
                tmp_path,
                _pg_receipt(tmp_path, state="pending"),
                [sys.executable, "-c", "import time; time.sleep(30)"],
                {},
            )
        receipt = pg._read(tmp_path)
        assert receipt is not None and receipt.state == "captured"
        assert receipt.process().pid == children[0].pid and receipt.process().live()
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_retained_postgres_cannot_signal_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path.parent / "run").mkdir(exist_ok=True)
    monkeypatch.setattr(pg, "observe", Mock(return_value=OwnedProcess(123, 100.0, 457)))
    monkeypatch.setattr(
        pg.OwnedProcess, "send_signal", Mock(side_effect=AssertionError("foreign signal"))
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        pg.start(
            tmp_path, 15433, [], {}, ready=lambda: True, expected=OwnedProcess(123, 100.0, 456)
        )


def _private_pg_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    from shared.config import settings
    from tests._containers import _free_port

    home, data = tmp_path / "home", tmp_path / "home/pg"
    home.mkdir()
    port = _free_port()
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
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


def _assert_warm_pg_repeat(data: Path, port: int, first: OwnedProcess, persisted: bytes) -> None:
    assert instance._start_pg(port, "") == 0
    repeated = ownership.postgres(data)
    assert repeated is not None and first.same_birth(repeated)
    assert pg.receipt_path(data).read_bytes() == persisted


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_owned_postgres_resume_and_fast_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary producer supplies all custody; no test-only receipt adoption."""
    import psycopg

    from cli.commands import _maintenance_data_plane as plane

    data, port = _private_pg_configuration(tmp_path, monkeypatch)
    try:
        assert instance._start_pg(port, "") == 0
        first, persisted = _assert_pg_birth(data, port)
        with psycopg.connect(instance.pg_admin_url(port), autocommit=True) as conn:
            ownership.require_postgres_connection(conn, data)
            row = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
            assert row is not None
            system_id = row[0]
            _assert_warm_pg_repeat(data, port, first, persisted)
            assert plane.stop(10) == ["postgres"]
            assert not first.live() and ownership.postgres(data) is None
            assert pg.receipt_path(data).read_bytes() == persisted
            with pytest.raises(psycopg.OperationalError):
                conn.execute("SELECT 1")
        assert plane.stop(2) == []
        assert instance._start_pg(port, "") == 0
        second, _receipt = _assert_pg_birth(data, port)
        assert not first.same_birth(second)
        with psycopg.connect(instance.pg_admin_url(port), autocommit=True) as conn:
            ownership.require_postgres_connection(conn, data)
            assert conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone() == (
                system_id,
            )
        with pytest.raises(RuntimeError, match="replacement"):
            pg.stop(data, expected=first)
        assert second.live()
    finally:
        pg.stop(data, timeout=10)
    assert ownership.postgres(data) is None
    ownership.require_listener(None, port, required=False)


def test_pg_native_signal_failure_is_not_reported_as_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from cli.commands import _maintenance_data_plane as plane
    from tests._containers import _free_port

    owner = OwnedProcess(123, 100.0, 456)
    seen: list[OwnedProcess | None] = []

    def fail(_data: Path, *, expected: OwnedProcess | None, timeout: float) -> None:
        assert timeout > 0
        seen.append(expected)
        raise RuntimeError("native custody lost")

    monkeypatch.setattr(plane.owned_postgres, "stop", fail)
    client = AsyncRedis(port=_free_port())
    with pytest.raises(RuntimeError, match="native custody lost"):
        asyncio.run(
            plane._request_stop("postgres", owner, client, plane.deadline_after(3), save=True)
        )
    assert seen == [owner]


def test_pgdata_symlink_cannot_adopt_another_homes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = tmp_path / "foreign/pg"
    foreign.mkdir(parents=True)
    hba = foreign / "pg_hba.conf"
    hba.write_text("foreign authority\n")
    local = tmp_path / "local/pg"
    local.parent.mkdir()
    local.symlink_to(foreign, target_is_directory=True)
    monkeypatch.setattr(instance, "_pg_data_dir", lambda: local)
    monkeypatch.setattr(pg, "_read", Mock(side_effect=AssertionError("foreign receipt read")))
    monkeypatch.setattr(
        instance, "_ensure_pg_data", Mock(side_effect=AssertionError("initdb effect"))
    )
    with pytest.raises(RuntimeError, match="symlink"):
        instance._start_pg(15433, "")
    assert hba.read_text() == "foreign authority\n"


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_stopped_postmaster_cannot_pass_warm_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, port = _private_pg_configuration(tmp_path, monkeypatch)
    try:
        assert instance._start_pg(port, "") == 0
        owner, before = _assert_pg_birth(data, port)
        assert owner.send_signal(signal.SIGSTOP)
        try:
            with monkeypatch.context() as context:
                context.setattr(instance, "_PG_START_TIMEOUT_S", 0.25)
                context.setattr(instance, "_PG_PROBE_TIMEOUT_S", 0.1)
                context.setattr(pg, "_spawn", Mock(side_effect=AssertionError("birth replacement")))
                with pytest.raises(RuntimeError, match="did not become ready"):
                    instance._start_pg(port, "")
            assert owner.live() and pg.receipt_path(data).read_bytes() == before
        finally:
            owner.send_signal(signal.SIGCONT)
        _assert_warm_pg_repeat(data, port, owner, before)
    finally:
        pg.stop(data, timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="owned POSIX PostgreSQL")
def test_real_interrupted_admission_completes_same_postmaster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
                instance._start_pg(port, "")
        captured = pg._read(data)
        assert captured is not None and captured.state == "captured"
        owner = captured.process()
        with monkeypatch.context() as context:
            context.setattr(pg, "_spawn", Mock(side_effect=AssertionError("birth replacement")))
            assert instance._start_pg(port, "") == 0
        admitted, persisted = _assert_pg_birth(data, port)
        assert owner.same_birth(admitted)
        _assert_warm_pg_repeat(data, port, owner, persisted)
    finally:
        pg.stop(data, timeout=10)
