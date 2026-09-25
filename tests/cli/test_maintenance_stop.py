"""Strict local maintenance stop uses real private processes, never force fallback."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import psutil
import pytest

from cli import commands
from cli.commands import _maintenance_data_plane as plane
from cli.commands import _maintenance_stop as stop
from cli.commands import _pgbouncer as pb
from cli.commands import _root_driver as root_driver
from shared.config import settings
from shared.session_backend import PosixProcSessionBackend, PtySessionBackend
from shared.session_record import SessionRecord, pid_starttime_ticks

Launcher = Callable[[str, str], subprocess.Popen[str]]


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unexpected force/signal path")


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="real POSIX signal contract")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    monkeypatch.setattr(stop, "get_shell_backend", lambda: SimpleNamespace(list_sessions=list))
    monkeypatch.setattr(root_driver, "_root_tree_selection", dict)
    monkeypatch.setattr(root_driver, "_stop_root_service_tree", lambda **_kwargs: 0)
    monkeypatch.setattr(commands, "_stop_root_service_tree", lambda **_kwargs: 0)
    monkeypatch.setattr(commands, "_root_tree_plan", lambda _preserve: [])

    def private_pty_cli(_self: PtySessionBackend, *tokens: str) -> subprocess.CompletedProcess[str]:
        # Stop intentionally consumes the ambient override. Every independent
        # test CLI still needs its explicit private binding when the checkout
        # currently points at an isolated native-proof home.
        return subprocess.run(  # noqa: S603 — fixed module and fixture-owned home
            [sys.executable, "-m", "shared.sessions.pty.cli", *tokens],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "AVA_HOME": str(tmp_path),
                "AVA_HOME_OVERRIDE": "1",
                "HOME": str(tmp_path),
            },
        )

    monkeypatch.setattr(PtySessionBackend, "_cli", private_pty_cli)
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
            pgid=os.getpgid(proc.pid),
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


def test_explicit_keep_preserves_real_idle_terminal_during_service_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.sessions.pty import cli as pty
    from shared.sessions.pty._paths import host_identity

    name = "ava-agent-123-shell-1"
    monkeypatch.setattr(stop, "get_shell_backend", PtySessionBackend)
    envfile = pty.write_env_file({})
    try:
        created = subprocess.run(  # noqa: S603 — test-owned home and repository module
            [sys.executable, "-m", "shared.sessions.pty.cli", name, "new", str(home), str(envfile)],
            env={**os.environ, "AVA_HOME": str(home), "AVA_HOME_OVERRIDE": "1", "HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        path = home / "run/pty" / f"{name}.json"
        record = SessionRecord.read(path)
        host = host_identity(path)
        assert record is not None and host is not None
        shell = stop.OwnedProcess(record.pid, record.create_time, record.starttime)
        terminal_host = stop.OwnedProcess.capture(psutil.Process(host[0]))
        deadline = time.monotonic() + 5
        while psutil.Process(record.pid).children(recursive=True):
            assert time.monotonic() < deadline, "terminal did not reach an idle shell"
            time.sleep(0.05)
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            root_driver, "_root_tree_selection", lambda: {"ava-agent-host": "agent-host"}
        )
        monkeypatch.setattr(root_driver, "_stop_root_service_tree", lambda **kw: calls.append(kw))
        with pytest.raises(RuntimeError, match="will not kill or replay"):
            stop.stop_services(3)
        assert not calls
        assert stop.stop_services(3, keep_terminals=True) == ["ava-agent-host"]
        assert len(calls) == 1 and calls[0]["force"] is False
        assert SessionRecord.read(path) == record and host_identity(path) == host
        assert shell.live() and terminal_host.live()
        assert PtySessionBackend().list_sessions() == [name]
    finally:
        # Only this fixture's named terminal is eligible for fixture cleanup.
        if name in pty.live_sessions():
            pty.session_request(name, {"op": "kill"})
        envfile.unlink(missing_ok=True)


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

    # OwnedProcess lives in shared.proc_tree (the stop path and the frontend
    # identity probe share it); patch the reference its live() consults.
    import shared.proc_tree

    monkeypatch.setattr(shared.proc_tree, "pid_starttime_ticks", read_tick)
    identity = stop.OwnedProcess(proc.pid, 0, 123)
    assert identity.live()
    tick = 124
    assert not identity.live()
    tick = None
    with pytest.raises(RuntimeError, match="cannot verify"):
        identity.live()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc start-time identity")
def test_wait_for_exit_converges_when_a_tracked_entry_vanishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked process exiting (reaped) mid-wait converges the wait instead
    of aborting the stop — the 2026-09-20 wave-2 failure: the identity read
    found no /proc entry after psutil had validated the pid."""
    import shared.proc_tree

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = child.pid
    starttime = pid_starttime_ticks(pid)
    assert starttime is not None
    identity = stop.OwnedProcess(pid, 0.5, starttime)
    real_read = pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        # The reap lands exactly where the kernel race puts it: after psutil
        # validated the pid, before the raw read completes.
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(shared.proc_tree, "pid_starttime_ticks", reaping_read)
    try:
        stop.wait_for_exit({identity}, stop.deadline_after(5))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


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


def test_pooler_stop_uses_wait_for_servers_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGINT is PgBouncer's safe shutdown (>=1.23: disconnect clients, wait only
    for in-flight server transactions). SIGTERM is the super-safe variant that
    waits for every client to disconnect — the 2026-09-12 hang under paused
    runners whose pooled clients never leave (issue #2307)."""
    identity = stop.OwnedProcess.capture(psutil.Process(os.getpid()))
    sent: list[int] = []

    class _RecordingProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def send_signal(self, sig: int) -> None:
            sent.append(sig)

    class _PsutilProxy:
        Process = _RecordingProcess

    monkeypatch.setattr(plane, "psutil", _PsutilProxy)
    plane._signal(identity)
    assert sent == [signal.SIGINT]


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
            client.set("owned-test", "old")
            client.save()  # pyright: ignore[reportUnknownMemberType] — redis stubs
            client.set("owned-test", "latest-unsaved")
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
        # Restart the exact private data directory. This proves SAVE includes
        # the latest in-memory write, not merely that an old RDB existed.
        from urllib.parse import urlparse

        from tests._containers import _wait_port

        port = urlparse(url).port
        assert port is not None
        restarted = subprocess.Popen(  # noqa: S603 — fixed binary and fixture-owned directory/port
            [
                "redis-server",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(data),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_port(port)
            with redis.Redis.from_url(url, decode_responses=True) as restored:  # pyright: ignore[reportUnknownMemberType]
                assert restored.get("owned-test") == "latest-unsaved"  # pyright: ignore[reportUnknownMemberType]
            assert stop.stop_data_plane(3) == ["redis"]
        finally:
            # SIGKILL: a shell session's SIGTERM=SIG_IGN is inherited, so the
            # graceful call would leave this restarted redis alive.
            if restarted.poll() is None:
                restarted.kill()
            restarted.wait(timeout=5)


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


def test_real_postgres_fast_stop_disconnects_open_client(
    local_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-m fast completes despite an idle client — the smart-stop twin of the
    pooler hang (issue #2307): the drain already proved no agent work is live,
    so waiting for an idle session to leave is not a safety condition."""
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
            assert stop.stop_data_plane(10) == ["postgres"]
            assert not (data / "postmaster.pid").exists()
            # The fast request disconnected the idle client rather than waiting
            # for it: the client's next use finds the backend gone.
            with pytest.raises(psycopg.OperationalError):
                client.execute("SELECT 1")
        assert stop.stop_data_plane(1) == []


def test_live_pty_host_with_dead_shell_blocks_stop(home: Path, launch: Launcher) -> None:
    from shared.sessions.pty._paths import write_record

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


def test_malformed_terminal_record_refuses_before_listing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "run/pty/unknown.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    monkeypatch.setattr(PosixProcSessionBackend, "list_sessions", forbidden)
    with pytest.raises(RuntimeError, match="cannot verify terminal record"):
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
    assert len(calls) == 1 and "fast" in calls[0] and "smart" not in calls[0]
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


def test_real_pgbouncer_stop_does_not_wait_for_idle_client(
    local_plane: None, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-12 incident shape: an idle connected client must not hold the
    pooler stop open.

    PgBouncer >=1.23 runs SIGTERM as SHUTDOWN WAIT_FOR_CLIENTS: with any
    connected client (here an idle admin console) it waits for every client to
    disconnect, so a stop under paused runners can only burn its deadline. The
    safe-shutdown SIGINT disconnects clients and waits only for in-flight
    server connections; it exits in well under a second."""
    import shutil

    import psycopg

    from tests._containers import _free_port, _wait_port

    binary = plane.pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native pgbouncer is not installed")
    directory = home / "pgbouncer"
    directory.mkdir()
    port = _free_port()
    (directory / "userlist.txt").write_text('"anyone" ""\n')
    ini = directory / "pgbouncer.ini"
    ini.write_text(
        "[databases]\n[pgbouncer]\nlisten_addr=127.0.0.1\n"
        f"listen_port={port}\nauth_type=trust\nauth_file={directory / 'userlist.txt'}\n"
        "admin_users=anyone\nstats_users=anyone\n"
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
        with psycopg.connect(
            f"postgresql://anyone@127.0.0.1:{port}/pgbouncer", autocommit=True
        ) as client:
            # Connected and idle from here on: the stop must not wait for this.
            assert client.execute("SHOW VERSION").fetchone() is not None
            assert stop.stop_data_plane(5) == ["pgbouncer"]
        assert not identity.live()
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


def _nonloopback_addr() -> str | None:
    """This machine's default-route address, or None when it has none.

    A UDP connect() picks the route without sending a packet (TEST-NET
    destination; connectionless sockets never contact it) — the portable read
    of "which local address would reach the network".
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addr = str(probe.getsockname()[0])
    except OSError:
        return None
    return addr if not addr.startswith("127.") else None


def _launch_incident_shape_pooler(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str, str, int]:
    """Start a real pooler on the incident's degraded-path config.

    Returns (port, role, secret, pid). Skips the calling test when this host
    cannot run the fixture. Walks the reachable-address code path the incident
    belongs to: a secret-set cluster binds loopback + the reachable address, so
    a listener-less pooler reads as degraded (a no-secret cluster short-circuits
    to the reload path by design). Pins the pooler's home and the address to
    private, bindable values.
    """
    from tests._containers import _free_port, _wait_port

    binary = pb.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native pgbouncer is not installed")
    addr = _nonloopback_addr()
    if addr is None:
        pytest.skip("this host has no non-loopback address for the reachable bind")

    monkeypatch.setattr(pb, "ava_home", lambda: home)
    monkeypatch.setattr(pb, "reachable_host", lambda: addr)
    monkeypatch.setattr(plane.instance, "reachable_host", lambda: addr)
    monkeypatch.setattr(pb, "_pg_socket_dir", lambda: home / "pg-socket")  # pyright: ignore[reportUnknownArgumentType] — private fixture home

    port = _free_port()
    role = "ava_maintenance_test"
    secret = "s3cr3t"  # noqa: S105 — private ephemeral fixture
    pb._write_config(
        pg_port=15433,
        listen_port=port,
        db_name="ava_maintenance_test",
        role=role,
        cluster_secret=secret,
        db_admin_password=secret,
        runner_role=None,
        runner_password="",
    )
    subprocess.run(  # noqa: S603 — private config, test-owned process
        [binary, "-d", str(pb._ini_path())], check=True, capture_output=True, timeout=5
    )
    try:
        _wait_port(port, timeout=5)
        return port, role, secret, _wait_pidfile()
    except BaseException:
        _kill_test_poolers()
        raise


def _wait_pidfile(timeout: float = 5.0) -> int:
    """The pooler pid once its pidfile lands.

    The daemon writes the pidfile asynchronously from binding its listeners:
    reading it straight after `_wait_port` returned races startup (2026-09-12,
    the run that leaked the pooler).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pb._pidfile_path().exists():
            with contextlib.suppress(ValueError):
                return int(pb._pidfile_path().read_text().strip())
        time.sleep(0.05)
    raise AssertionError("the pooler never wrote its pidfile")


def _pidfile_pid() -> set[int]:
    """The pid recorded in the (test-home) pooler pidfile, when readable."""
    pidfile = pb._pidfile_path()
    if not pidfile.exists():
        return set()
    with contextlib.suppress(ValueError):
        return {int(pidfile.read_text().strip())}
    return set()


def _kill_test_poolers(*pids: int | None) -> None:
    """Kill exactly the test-owned pooler instances, half-started ones included."""
    for target in sorted({p for p in pids if p} | _pidfile_pid()):
        if psutil.pid_exists(target):
            with contextlib.suppress(psutil.NoSuchProcess):
                proc = psutil.Process(target)
                proc.kill()
                proc.wait(timeout=5)


def test_real_start_retains_pooler_with_held_client(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compensating normal start cannot force a pooler past its held drain."""
    import psycopg

    port, role, secret, pid = _launch_incident_shape_pooler(home, monkeypatch)
    old = stop.OwnedProcess.capture(psutil.Process(pid))
    config = (pb._ini_path().read_bytes(), pb._userlist_path().read_bytes())
    try:
        with psycopg.connect(
            f"postgresql://{role}:{secret}@127.0.0.1:{port}/pgbouncer", autocommit=True
        ) as client:
            assert client.execute("SHOW VERSION").fetchone() is not None
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not pb.pgbouncer_public_listener_reachable(port, role, secret):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("the pooler never closed its listeners after SIGTERM")
            assert old.live()
            with pytest.raises(RuntimeError, match="custody retained"):
                pb.ensure_pgbouncer(
                    pg_port=15433,
                    listen_port=port,
                    db_name="ava_maintenance_test",
                    role=role,
                    cluster_secret=secret,
                    db_admin_password=secret,
                    runner_password="",
                )
            assert old.live(), "normal start must retain a pooler still draining its client"
            assert pb._running_pid() == pid, "no replacement may be launched"
            assert (pb._ini_path().read_bytes(), pb._userlist_path().read_bytes()) == config
    finally:
        _kill_test_poolers(pid)


def test_selected_service_stop_delegates_exact_units_to_root(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        root_driver,
        "_root_tree_selection",
        lambda: {"ava-gateway": "gateway", "ava-agent-host": "agent-host"},
    )
    monkeypatch.setattr(root_driver, "_stop_root_service_tree", lambda **kw: calls.append(kw))
    assert stop.stop_services(3, selected=frozenset({"ava-agent-host"})) == ["ava-agent-host"]
    assert len(calls) == 1
    assert calls[0]["preserve"] == frozenset({"gateway"})
    assert calls[0]["selected"] == frozenset({"agent-host"})
    assert calls[0]["force"] is False
    timeout = calls[0]["timeout_s"]
    assert isinstance(timeout, float) and 0 < timeout <= 3


def test_root_stop_failure_is_not_reported_as_success(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(root_driver, "_root_tree_selection", lambda: {"ava-gateway": "gateway"})

    def refuse(**_kwargs: object) -> None:
        raise RuntimeError("root custody unavailable")

    monkeypatch.setattr(root_driver, "_stop_root_service_tree", refuse)
    with pytest.raises(RuntimeError, match="root custody unavailable"):
        stop.stop_services(3)
