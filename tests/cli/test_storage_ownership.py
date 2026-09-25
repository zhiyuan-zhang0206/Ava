"""Native readiness never authorizes changes to a foreign data-plane listener."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlsplit

import pytest
import redis
from redis.asyncio import Redis as AsyncRedis

from cli.commands import _cluster_instance as instance
from cli.commands import _pgbouncer as pooler
from shared.cluster import ownership
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


def test_postmaster_pidfile_birth_must_match_native_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "postmaster.pid").write_text(f"123\n{tmp_path}\n100\n")
    monkeypatch.setattr(
        ownership,
        "process_identity",
        lambda _pid: SimpleNamespace(birth=200, live=lambda: True),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(
        ownership,
        "psutil",
        SimpleNamespace(
            Process=lambda _pid: SimpleNamespace(
                name=lambda: "postgres",
                cmdline=lambda: ["postgres", "-D", str(tmp_path)],
            )
        ),
    )
    with pytest.raises(RuntimeError, match="cannot verify this home's PostgreSQL"):
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
    capture = plane._capture_redis

    async def disconnect_after_capture(
        client: AsyncRedis, deadline: float, port: int, custody: ownership.RedisConnectionCustody
    ) -> ownership.OwnedProcess | None:
        result = await capture(client, deadline, port, custody)
        assert client.connection is not None
        await client.connection.disconnect()  # pyright: ignore[reportUnknownMemberType] — redis stubs
        return result

    monkeypatch.setattr(plane, "_capture_redis", disconnect_after_capture)
    with redis_server() as url:
        monkeypatch.setattr(settings.data_plane, "redis_url", url)
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            directory = Path(str(client.config_get("dir")["dir"]))  # pyright: ignore[reportUnknownMemberType] — redis stubs
            monkeypatch.setattr(plane.instance, "_redis_data_dir", lambda: directory)
            with pytest.raises(RuntimeError, match="connection changed"):
                plane.stop(3)
            assert client.ping(), "the server must survive lost connection custody"  # pyright: ignore[reportUnknownMemberType] — redis stubs
