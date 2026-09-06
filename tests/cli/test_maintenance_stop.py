"""Strict local maintenance stop uses real private processes, never force fallback."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import psutil
import pytest

from cli.commands import _maintenance_data_plane as plane
from cli.commands import _maintenance_stop as stop
from shared.config import settings
from shared.session_backend import PosixProcSessionBackend
from shared.session_record import SessionRecord, pid_starttime_ticks

Launcher = Callable[[str, str], subprocess.Popen[str]]


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unexpected force/signal path")


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="real POSIX signal contract")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    monkeypatch.setattr(stop, "get_backend", PosixProcSessionBackend)
    monkeypatch.setattr(stop, "get_shell_backend", lambda: SimpleNamespace(list_sessions=list))
    return tmp_path


@pytest.fixture
def launch(home: Path) -> Iterator[Callable[[str, str], subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []

    def create(name: str, code: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(  # noqa: S603 — test-owned Python and fixed fixture scripts
            [sys.executable, "-u", "-c", code],
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(proc)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        SessionRecord(
            proc.pid,
            psutil.Process(proc.pid).create_time(),
            "private-test",
            str(home),
            time.time(),
            pid_starttime_ticks(proc.pid),
        ).write(home / "run/sessions" / f"{name}.json")
        return proc

    yield create
    for proc in processes:
        if proc.poll() is None:
            # Test fixture cleanup alone may kill the exact private process group
            # it created, after the assertions prove strict stop left it alive.
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


_EXIT = "import time; print('ready', flush=True); time.sleep(60)"
_IGNORE = (
    "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)"
)


def test_service_exits_normally_and_does_not_touch_sibling(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = home / "completed"
    code = (
        "import signal,time,pathlib; "
        f"signal.signal(signal.SIGTERM,lambda *_: (pathlib.Path({str(marker)!r}).write_text('done'), exit(0))); "
        "print('ready',flush=True); time.sleep(60)"
    )
    proc = launch("ava-agent-host", code)
    sibling_home = home.with_name(home.name + "-sibling")
    sibling_home.mkdir()
    sibling = launch("sibling", f"import os; os.chdir({str(sibling_home)!r}); " + _IGNORE)
    path = home / "run/sessions/sibling.json"
    sibling_record = SessionRecord.read(path)
    assert sibling_record is not None
    replace(sibling_record, cwd=str(sibling_home)).write(sibling_home / "run/sessions/sibling.json")
    path.unlink()
    assert Path(psutil.Process(sibling.pid).cwd()) == sibling_home
    monkeypatch.setattr(PosixProcSessionBackend, "kill_session", forbidden)
    assert stop.stop_services(3) == ["ava-agent-host"]
    assert proc.wait(timeout=1) == 0
    assert marker.read_text() == "done"
    assert sibling.poll() is None
    assert stop.stop_services(1) == []


def test_timeout_leaves_both_services_alive_under_one_deadline(launch: Launcher) -> None:
    first, second = launch("first", _IGNORE), launch("second", _IGNORE)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="kept its hold"):
        stop.stop_services(0.15)
    assert time.monotonic() - started < 0.65
    assert first.poll() is None and second.poll() is None


def test_orphaned_captured_descendant_prevents_success(launch: Launcher) -> None:
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print('ready',flush=True); time.sleep(60)"
    )
    parent = launch("parent", code)
    children = psutil.Process(parent.pid).children()
    assert len(children) == 1
    try:
        with pytest.raises(TimeoutError, match="did not exit"):
            stop.stop_services(0.15)
        assert parent.wait(timeout=1) == -signal.SIGTERM
        assert children[0].is_running()
    finally:
        children[0].kill()  # Exact child created by this fixture, not production.


def test_invalid_identity_refuses_every_signal(home: Path, launch: Launcher) -> None:
    first, other = launch("a-first", _IGNORE), launch("z-invalid", _IGNORE)
    path = home / "run/sessions/z-invalid.json"
    record = SessionRecord.read(path)
    assert record is not None
    replace(record, create_time=record.create_time + 1, starttime=None).write(path)
    with pytest.raises(RuntimeError, match="identity changed"):
        stop.stop_services(1)
    assert first.poll() is None and other.poll() is None


def test_persistent_terminals_refuse_before_signalling(
    launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = launch("ava-agent-host", _EXIT)
    monkeypatch.setattr(
        stop, "get_shell_backend", lambda: SimpleNamespace(list_sessions=lambda: ["schedule-8"])
    )
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        stop.stop_services(1)
    assert proc.poll() is None


def test_replaced_record_is_not_signalled(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, replacement = launch("service", _IGNORE), launch("replacement", _IGNORE)
    replacement_path = home / "run/sessions/replacement.json"
    record = SessionRecord.read(replacement_path)
    assert record is not None
    replacement_path.unlink()
    actual = PosixProcSessionBackend.graceful_signal

    def replace_then_signal(
        self: PosixProcSessionBackend, name: str, *, expected: SessionRecord | None = None
    ) -> bool:
        record.write(home / "run/sessions/service.json")
        return actual(self, name, expected=expected)

    monkeypatch.setattr(PosixProcSessionBackend, "graceful_signal", replace_then_signal)
    with pytest.raises(RuntimeError, match="signal refused"):
        stop.stop_services(1)
    assert original.poll() is None and replacement.poll() is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_refuses(timeout: float, home: Path) -> None:
    with pytest.raises(ValueError):
        stop.stop_services(timeout)
    with pytest.raises(ValueError):
        stop.stop_data_plane(timeout)


def test_linux_ticks_win_over_changed_epoch_birth(
    launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = launch("unused", _IGNORE)
    tick: int | None = 123

    def read_tick(_pid: int) -> int | None:
        return tick

    monkeypatch.setattr(stop, "pid_starttime_ticks", read_tick)
    identity = stop.OwnedProcess(proc.pid, 0, 123)
    assert identity.live()
    tick = 124
    assert not identity.live()
    tick = None
    with pytest.raises(RuntimeError, match="cannot verify"):
        identity.live()


@pytest.fixture
def local_plane(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://test@127.0.0.1:12345/test")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://127.0.0.1:12346")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")


def test_remote_plane_refuses_without_any_signal(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://192.0.2.4:6379")
    monkeypatch.setattr(plane, "_capture_postgres", lambda: pytest.fail("local scan"))
    with pytest.raises(RuntimeError, match="remote-managed"):
        stop.stop_data_plane(1)


def test_recycled_pooler_pid_is_not_stopped(local_plane: None, home: Path) -> None:
    path = home / "pgbouncer/pgbouncer.pid"
    path.parent.mkdir()
    path.write_text(str(os.getpid()))
    with pytest.raises(RuntimeError, match="PgBouncer"):
        stop.stop_data_plane(1)
    assert path.exists()


def test_real_redis_stops_owned_instance_only(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    from tests._containers import redis_server

    with redis_server() as url, redis_server() as sibling_url:
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            directory = client.config_get("dir")["dir"]  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert isinstance(directory, str)
            data = Path(directory)
            pid = int(client.info("server")["process_id"])  # pyright: ignore[reportUnknownMemberType] — redis stubs
            client.set("owned-test", "present")
        monkeypatch.setattr(settings.data_plane, "redis_url", url)
        monkeypatch.setattr(plane.instance, "_redis_data_dir", lambda: data)
        assert stop.stop_data_plane(3) == ["redis"]
        assert (
            not stop.OwnedProcess.capture(psutil.Process(pid)).live()
            if psutil.pid_exists(pid)
            else True
        )
        with redis.Redis.from_url(sibling_url) as sibling:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert sibling.ping()  # pyright: ignore[reportUnknownMemberType] — redis stubs
        assert stop.stop_data_plane(1) == []


def test_foreign_redis_directory_refuses_before_local_signals(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    from tests._containers import redis_server

    with redis_server() as url:
        monkeypatch.setattr(settings.data_plane, "redis_url", url)
        monkeypatch.setattr(plane, "_signal", forbidden)
        with pytest.raises(RuntimeError, match="Redis process"):
            stop.stop_data_plane(2)
        with redis.Redis.from_url(url) as client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert client.ping()  # pyright: ignore[reportUnknownMemberType] — redis stubs


def test_real_postgres_smart_stop_waits_for_open_client(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from shared.pg_tools import pg_tool, throwaway_postgres
    from tests._containers import _free_port

    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")

    def binary(name: str) -> str:
        return str(pg_tool(name))

    monkeypatch.setattr(plane.instance, "_pg_bin", binary)
    with throwaway_postgres() as url:
        with psycopg.connect(url, autocommit=True) as client:
            row = client.execute("SHOW data_directory").fetchone()
            assert row is not None
            data = Path(row[0])
            monkeypatch.setattr(plane.instance, "_pg_data_dir", lambda: data)
            pid = int((data / "postmaster.pid").read_text().splitlines()[0])
            with pytest.raises(TimeoutError):
                stop.stop_data_plane(1)
            assert client.execute("SELECT 1").fetchone() == (1,)
            assert psutil.pid_exists(pid)
        # The first SMART request takes effect when the existing client leaves.
        deadline = time.monotonic() + 4
        while (data / "postmaster.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not (data / "postmaster.pid").exists()
        assert stop.stop_data_plane(1) == []


def test_live_pty_host_with_dead_shell_blocks_stop(home: Path, launch: Launcher) -> None:
    from shared.pty_sessions._paths import write_record

    proc = launch("temporary-host", _IGNORE)
    (home / "run/sessions/temporary-host.json").unlink()
    write_record(
        home / "run/pty/ava-agent-123-shell-1.json",
        SessionRecord(proc.pid, 0, "private-fixture", str(home), 0),
        host_pid=proc.pid,
        host_create_time=psutil.Process(proc.pid).create_time(),
        host_starttime=pid_starttime_ticks(proc.pid),
    )
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        stop.stop_services(1)
    assert proc.poll() is None


def test_malformed_record_refuses_before_listing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "run/sessions/unknown.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    monkeypatch.setattr(PosixProcSessionBackend, "list_sessions", forbidden)
    with pytest.raises(RuntimeError, match="cannot verify service record"):
        stop.stop_services(1)
    assert path.read_text() == "{"


def test_redis_admin_credential_is_independent_of_runtime_url(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    from tests._containers import redis_server

    password = "private-maintenance-test"  # noqa: S105 — private ephemeral test
    with redis_server() as url:
        with redis.Redis.from_url(url, decode_responses=True) as client:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            directory = client.config_get("dir")["dir"]  # pyright: ignore[reportUnknownMemberType] — redis stubs
            assert isinstance(directory, str)
            client.config_set("requirepass", password)  # pyright: ignore[reportUnknownMemberType] — redis stubs
        monkeypatch.setattr(
            settings.data_plane, "redis_url", url.replace("redis://", "redis://restricted:wrong@")
        )
        monkeypatch.setattr(settings.data_plane, "redis_admin_password", password)
        monkeypatch.setattr(plane.instance, "_redis_data_dir", lambda: Path(directory))
        assert stop.stop_data_plane(3) == ["redis"]


def test_pg_ctl_failure_is_not_reported_as_stopped(
    local_plane: None, home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._containers import _free_port

    proc = launch("owned-standin", _IGNORE)
    identity = stop.OwnedProcess.capture(psutil.Process(proc.pid))
    monkeypatch.setattr(plane, "_capture_postgres", lambda: identity)
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    calls: list[list[str]] = []

    def failed(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 7)

    with monkeypatch.context() as context:
        context.setattr(plane.subprocess, "run", failed)
        with pytest.raises(RuntimeError, match="exit 7"):
            stop.stop_data_plane(1)
    assert len(calls) == 1 and "smart" in calls[0] and "fast" not in calls[0]
    assert proc.poll() is None


def test_real_pgbouncer_normal_exit_and_identity_cleanup(
    local_plane: None, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from tests._containers import _free_port, _wait_port

    binary = plane.pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native pgbouncer is not installed")
    directory = home / "pgbouncer"
    directory.mkdir()
    port = _free_port()
    ini = directory / "pgbouncer.ini"
    ini.write_text(
        "[databases]\n[pgbouncer]\nlisten_addr=127.0.0.1\n"
        f"listen_port={port}\nauth_type=trust\n"
        f"pidfile={directory / 'pgbouncer.pid'}\n"
        f"logfile={directory / 'pgbouncer.log'}\n"
        "unix_socket_dir=\n"
    )
    subprocess.run([binary, "-d", str(ini)], check=True, capture_output=True, timeout=5)  # noqa: S603 — private config
    _wait_port(port, timeout=5)
    pid = int((directory / "pgbouncer.pid").read_text())
    identity = stop.OwnedProcess.capture(psutil.Process(pid))
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    try:
        assert stop.stop_data_plane(3) == ["pgbouncer"]
        assert not identity.live()
        assert not (directory / "pgbouncer.pid").exists()
    finally:
        if identity.live():
            psutil.Process(pid).kill()  # Exact test-owned process only, after assertions.


def test_missing_pooler_pidfile_does_not_mean_the_process_is_gone(
    local_plane: None, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._containers import _free_port

    identity = stop.OwnedProcess.capture(psutil.Process())
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    # The scanner sees an exact owned pooler but its pidfile is absent. No stop
    # signal can be justified; the test's actual process must remain untouched.
    process = psutil.Process()
    process.info = {"pid": identity.pid, "name": "pgbouncer"}

    def processes(_attrs: list[str]) -> Iterator[psutil.Process]:
        yield process

    monkeypatch.setattr(plane.psutil, "process_iter", processes)
    monkeypatch.setattr(plane.pooler, "_pid_is_our_pooler", lambda _pid: True)  # pyright: ignore[reportUnknownArgumentType] — constant identity fixture
    monkeypatch.setattr(plane, "_signal", forbidden)
    with pytest.raises(RuntimeError, match="unrecorded or replacement"):
        stop.stop_data_plane(1)


def test_redis_cleanup_cannot_turn_deadline_into_an_unbounded_wait(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from tests._containers import _free_port

    class HangingClose:
        connection = None

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def aclose(self) -> None:
            await asyncio.Future()

    monkeypatch.setattr(plane, "Redis", HangingClose)
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        stop.stop_data_plane(0.1)
    assert time.monotonic() - started < 0.7
