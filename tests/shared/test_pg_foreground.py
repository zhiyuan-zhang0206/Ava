"""Foreground restore databases remain owned and cannot escape shutdown."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psutil
import psycopg
import pytest

from shared import pg_foreground, pg_throwaway_base, pg_tools
from shared.config import settings
from shared.native_process.ownership import OwnedProcess
from shared.platform import IS_WINDOWS

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="foreground restore ownership is POSIX")


@pytest.fixture
def foreground_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Use short socket paths and restrict every sweep to this test's instances."""
    with tempfile.TemporaryDirectory(prefix="ava-fg-", dir="/tmp") as directory:
        root = Path(directory)
        monkeypatch.setattr(pg_throwaway_base, "_tmpfs_base", str(root))
        monkeypatch.setattr(pg_throwaway_base, "disk_fallback_base", lambda: root)
        monkeypatch.setattr(settings.data_plane, "pg_throwaway_base", "")
        try:
            yield root
        finally:
            # Independent cleanup also runs when an assertion inside the context fails.
            for data in root.glob("ava-pg-*/data"):
                if (data / "PG_VERSION").is_file():
                    subprocess.run(  # noqa: S603 -- only this test's own temporary PGDATA
                        [
                            pg_tools.pg_tool("pg_ctl"),
                            "-D",
                            str(data),
                            "-m",
                            "immediate",
                            "-t",
                            "2",
                            "stop",
                        ],
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_foreground_is_direct_child_and_reaped_on_context_exit(
    foreground_root: Path, interrupted: bool
) -> None:
    """Observe the real postmaster, including the BaseException cleanup path."""
    pid = 0
    instance = foreground_root
    outcome = pytest.raises(KeyboardInterrupt) if interrupted else nullcontext()
    with outcome, pg_tools.throwaway_postgres(base=foreground_root, foreground=True) as url:
        with psycopg.connect(url) as connection:
            row = connection.execute("SHOW data_directory").fetchone()
            assert row is not None
            data = Path(row[0])
            instance = data.parent
            pid = int((data / "postmaster.pid").read_text().splitlines()[0])
            assert psutil.Process(pid).ppid() == os.getpid()
            assert os.getpgid(pid) == os.getpgrp()
            assert connection.execute("SELECT 1").fetchone() == (1,)
            assert connection.execute("SHOW timezone").fetchone() == ("UTC",)
            assert connection.execute("SHOW shared_memory_type").fetchone() == ("mmap",)
            assert connection.execute("SHOW dynamic_shared_memory_type").fetchone() == ("mmap",)
        if interrupted:
            raise KeyboardInterrupt

    assert pid > 0
    assert not instance.exists()
    assert not psutil.pid_exists(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not list(foreground_root.glob("ava-pg-*"))


def test_startup_failure_reaps_the_native_postmaster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid native invocation must fail with its direct child already reaped."""

    def no_connection(**_kwargs: Any) -> None:
        raise psycopg.OperationalError("test child cannot listen")

    with monkeypatch.context() as scoped:
        scoped.setattr(pg_foreground.psycopg, "connect", no_connection)
        log = tmp_path / "pg.log"
        process = pg_foreground.start_foreground_postgres(
            [str(pg_tools.pg_tool("postgres")), "--ava-invalid-start-option"], log=log
        )
        try:
            with pytest.raises(RuntimeError, match="foreground Postgres exited"):
                pg_foreground.wait_foreground_postgres(
                    process,
                    data=tmp_path / "data",
                    log=log,
                    port=1,
                    timeout_s=5,
                )
            assert process.returncode is not None and process.returncode != 0
            with pytest.raises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        finally:
            pg_foreground.stop_foreground_postgres(process)


def test_readiness_rejects_an_unrelated_postgres_on_the_selected_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port collision must never authorize creating roles in the other server."""
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.return_value = 0
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (str(tmp_path / "other-db"),)
    monkeypatch.setattr(pg_foreground.psycopg, "connect", MagicMock(return_value=connection))

    with pytest.raises(RuntimeError, match="another data directory"):
        pg_foreground.wait_foreground_postgres(
            process, data=tmp_path / "owned-db", log=tmp_path / "pg.log", port=1
        )

    assert all("CREATE" not in str(call).upper() for call in connection.execute.call_args_list)


def test_readiness_timeout_is_bounded(tmp_path: Path) -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None

    with pytest.raises(TimeoutError, match="did not become ready"):
        pg_foreground.wait_foreground_postgres(
            process,
            data=tmp_path / "data",
            log=tmp_path / "pg.log",
            port=1,
            timeout_s=0,
        )


@pytest.fixture
def fake_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mocked postmaster has no native family and never finishes its shutdown."""

    def no_family(_process: subprocess.Popen[bytes]) -> set[OwnedProcess]:
        return set()

    monkeypatch.setattr(pg_foreground, "postmaster_family", no_family)
    monkeypatch.setattr(pg_foreground, "POSTMASTER_SHUTDOWN_S", 0.0)


@pytest.mark.usefixtures("fake_family")
def test_stop_escalates_and_reaps_after_immediate_shutdown_times_out() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.return_value = -signal.SIGKILL

    pg_foreground.stop_foreground_postgres(process)

    process.send_signal.assert_called_once_with(signal.SIGQUIT)
    process.kill.assert_called_once()
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [2]


@pytest.mark.usefixtures("fake_family")
def test_unreapable_foreground_retains_data_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = tmp_path / "ava-pg-owned"
    data = instance / "data"
    data.mkdir(parents=True)
    sentinel = data / "retained"
    sentinel.write_text("must survive unconfirmed shutdown")
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("postgres", 2)
    unregister = MagicMock()
    monkeypatch.setattr(pg_tools, "_unregister_throwaway", unregister)

    with pytest.raises(subprocess.TimeoutExpired):
        pg_tools._teardown_throwaway(instance, data, None, process, foreground=True)

    assert sentinel.read_text() == "must survive unconfirmed shutdown"
    unregister.assert_not_called()
    process.kill.assert_called_once()


@pytest.mark.usefixtures("fake_family")
@pytest.mark.parametrize("stop_fails", [False, True])
def test_startup_timeout_keeps_the_child_handle_for_cleanup(
    foreground_root: Path, monkeypatch: pytest.MonkeyPatch, stop_fails: bool
) -> None:
    """Even readiness failure must retain live PGDATA until its owner can reap it."""
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.return_value = 0
    if stop_fails:
        process.wait.side_effect = subprocess.TimeoutExpired("postgres", 2)
    registrations: list[pg_tools._Registration] = []
    register = pg_tools._register_throwaway
    unregister = pg_tools._unregister_throwaway
    released: list[pg_tools._Registration | None] = []

    def capture_registration(path: Path, port: int) -> pg_tools._Registration | None:
        registration = register(path, port)
        if registration is not None:
            registrations.append(registration)
        return registration

    def release_registration(registration: pg_tools._Registration | None) -> None:
        released.append(registration)
        unregister(registration)

    def never_ready(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError("injected readiness deadline")

    monkeypatch.setattr(pg_tools, "_register_throwaway", capture_registration)
    monkeypatch.setattr(pg_tools, "_unregister_throwaway", release_registration)
    monkeypatch.setattr(pg_tools, "start_foreground_postgres", MagicMock(return_value=process))
    monkeypatch.setattr(pg_tools, "wait_foreground_postgres", never_ready)
    error = subprocess.TimeoutExpired if stop_fails else TimeoutError
    try:
        with (
            pytest.raises(error),
            pg_tools.throwaway_postgres(base=foreground_root, foreground=True),
        ):
            pytest.fail("a failed readiness check must never yield a database URL")
        process.send_signal.assert_called_once_with(signal.SIGQUIT)
        assert len(registrations) == 1
        if stop_fails:
            [instance] = list(foreground_root.glob("ava-pg-*"))
            assert (instance / "data" / "PG_VERSION").is_file()
            assert (instance / "owner.lock").is_file()
            assert not released
            process.kill.assert_called_once()
        else:
            assert not list(foreground_root.glob("ava-pg-*"))
            assert released == registrations
    finally:
        # The fake refuses to die; release only the locks created by this test.
        for registration in registrations:
            if registration not in released:
                unregister(registration)


@pytest.mark.usefixtures("fake_family")
def test_foreground_start_is_handed_the_built_start_env(
    foreground_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #3754: the direct `postgres` spawn receives the same explicit start
    env as the pg_ctl path (see pg_tools.pg_start_env) — the postmaster is
    never started without a locale."""
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.return_value = 0
    spawn = MagicMock(return_value=process)
    sentinel = {"LC_ALL": "en_US.UTF-8"}
    monkeypatch.setattr(pg_tools, "pg_start_env", lambda: sentinel)
    monkeypatch.setattr(pg_tools, "start_foreground_postgres", spawn)

    def never_ready(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError("injected readiness deadline")

    monkeypatch.setattr(pg_tools, "wait_foreground_postgres", never_ready)

    with (
        pytest.raises(TimeoutError),
        pg_tools.throwaway_postgres(base=foreground_root, foreground=True),
    ):
        pytest.fail("readiness never succeeds in this test")

    assert spawn.call_args.kwargs["env"] == sentinel


def test_stop_kills_the_recorded_family_of_a_postmaster_that_cannot_shut_down(
    foreground_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every postmaster child setsid()s out of its owner's group. A postmaster
    that ignores its shutdown and is killed must not orphan them: a busy backend
    would otherwise run its whole statement against the disposable PGDATA."""
    data = foreground_root / "data"
    subprocess.run(  # noqa: S603 -- disposable test cluster
        [pg_tools.pg_tool("initdb"), "-D", str(data), "-U", "ava", "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
    )
    port = pg_tools._free_port()
    log = foreground_root / "pg.log"
    process = pg_foreground.start_foreground_postgres(
        [
            str(pg_tools.pg_tool("postgres")),
            "-D",
            str(data),
            "-p",
            str(port),
            "-k",
            str(foreground_root),
            "-c",
            "listen_addresses=127.0.0.1",
        ],
        log=log,
    )
    url = f"postgresql://ava@127.0.0.1:{port}/postgres"
    busy = "SELECT count(*) FROM generate_series(1, 20000000000)"
    client: subprocess.Popen[bytes] | None = None
    family: list[OwnedProcess] = []
    try:
        pg_foreground.wait_foreground_postgres(process, log=log, port=port, data=data)
        client = subprocess.Popen(  # noqa: S603 -- disposable busy client
            [
                sys.executable,
                "-c",
                "import psycopg,sys; psycopg.connect(sys.argv[1]).execute(sys.argv[2])",
                url,
                busy,
            ]
        )
        with psycopg.connect(url, autocommit=True) as probe:
            while not probe.execute(
                "SELECT 1 FROM pg_stat_activity WHERE state = 'active' AND query = %s", (busy,)
            ).fetchone():
                time.sleep(0.05)
        family = [
            OwnedProcess.capture(child)
            for child in psutil.Process(process.pid).children(recursive=True)
        ]
        os.kill(process.pid, signal.SIGSTOP)  # this postmaster never finishes its shutdown
        monkeypatch.setattr(pg_foreground, "POSTMASTER_SHUTDOWN_S", 0.5, raising=False)
        pg_foreground.stop_foreground_postgres(process)
        assert process.returncode is not None
        assert [member.pid for member in family if member.live()] == []
    finally:
        for member in family:
            with contextlib.suppress(Exception):
                member.send_signal(signal.SIGKILL)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if client is not None:
            client.kill()
            client.wait(timeout=5)


def test_a_retried_family_closure_keeps_every_earlier_recorded_birth(tmp_path: Path) -> None:
    """A closure retried after an unresolved attempt still kills and proves the
    births that attempt persisted, not only what it can record anew."""
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        member = OwnedProcess.capture(psutil.Process(survivor.pid))
        (tmp_path / pg_foreground.FAMILY_RECORD).write_text(
            json.dumps({"members": [pg_foreground.birth_value(member)]})
        )
        family = pg_foreground.FamilyCustody(tmp_path, os.getpgrp(), None)
        family.close(time.monotonic() + 5)
        assert not member.live()
    finally:
        survivor.kill()
        survivor.wait(timeout=10)
