"""Real PTY sessions die with their test: `PtyReaper` and the `pty_reaper` fixture.

Registered for the CLI tests by `tests/path_scoped/cli_tests.py`; tests elsewhere
that run real PTY sessions import both names from here.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
from collections.abc import Iterator

import psutil
import pytest

from base.native_process.ownership import OwnedProcess
from base.paths import run_dir
from base.sessions.backend import PtySessionBackend
from base.sessions.record import SessionRecord


def _running(process: psutil.Process) -> bool:
    """Still the pinned process and not yet exited (a zombie has exited)."""
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _freeze(roots: list[psutil.Process]) -> list[psutil.Process]:
    """SIGSTOP each tree top-down; return every process stopped.

    A stopped process cannot fork, so the children listed after their parent
    stopped are final: nothing escapes between capture and kill — not a job
    the shell is still spawning, not a loop's next child.
    """
    frozen: dict[int, psutil.Process] = {}
    pending = list(roots)
    while pending:
        process = pending.pop()
        if process.pid in frozen:
            continue
        try:
            process.suspend()
        except psutil.NoSuchProcess:
            continue  # exited, or its pid now names another process
        frozen[process.pid] = process
        with contextlib.suppress(psutil.NoSuchProcess):
            pending.extend(process.children())
    return list(frozen.values())


class PtyReaper:
    """SIGKILLs every process a test's real PTY sessions created, pass or fail.

    Teardown cannot route through the code under test: once a stop HUPs a
    session's shell, the host ends the session (record and socket gone), and
    ``kill_session(name)`` no longer reaches a job that ignored the hangup —
    it lives on as an orphan of init. The processes are pinned instead, as
    ``psutil.Process`` objects, which refuse to signal a recycled pid.
    """

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self._tmp = str(tmp_path)
        self._names: list[str] = []
        self._pinned: list[psutil.Process] = []

    def track_session(self, name: str) -> OwnedProcess:
        """Pin a just-created session's host and shell; return the shell."""
        path = run_dir() / "pty" / f"{name}.json"
        record = SessionRecord.read(path)
        assert record is not None, f"session {name} left no record"
        raw = json.loads(path.read_text(encoding="utf-8"))
        host = OwnedProcess(raw["host_pid"], raw["host_create_time"], raw["host_starttime"])
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        self._names.append(name)
        for identity in (host, shell):
            # Construct first, then verify: a pid recycled in between fails the check.
            process = psutil.Process(identity.pid)
            assert identity.live(), f"session {name}: pid {identity.pid} is not the recorded one"
            self._pinned.append(process)
        return shell

    def track(self, *processes: psutil.Process) -> None:
        """Pin processes the test observed — a job can outlive its shell."""
        self._pinned.extend(processes)

    def _strays(self) -> list[psutil.Process]:
        """Live processes whose argv names this test's private tmp dir.

        The PTY host carries it on argv, and so does a file-backed job even
        when no test step captured it. The dir is unique to this test, so a
        match is this test's process.
        """
        prefix = self._tmp + os.sep
        return [
            process
            for process in psutil.process_iter(["cmdline"])
            if process.pid != os.getpid()
            and any(
                arg == self._tmp or arg.startswith(prefix) for arg in process.info["cmdline"] or ()
            )
            and _running(process)
        ]

    def reap(self) -> None:
        """SIGKILL the frozen trees, then fail the test if anything survived."""
        frozen = _freeze([*self._pinned, *self._strays()])
        for process in frozen:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
        deadline = time.monotonic() + 10
        while any(_running(process) for process in frozen) and time.monotonic() < deadline:
            time.sleep(0.05)
        # The listing sweeps the dead sessions' records and sockets (a long
        # home puts the socket outside tmp_path) and must not list ours.
        listed = set(PtySessionBackend().list_sessions()) & set(self._names)
        survivors = {process.pid for process in frozen if _running(process)}
        survivors |= {process.pid for process in self._strays()}
        if survivors or listed:
            pytest.fail(
                f"PTY test processes survived teardown: pids={sorted(survivors)}, sessions={sorted(listed)}"
            )


@pytest.fixture
def pty_reaper(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PtyReaper]:
    """Real PTY sessions die with their test; see ``PtyReaper``.

    Depends on ``monkeypatch`` only for teardown order: the reap must run
    while the test's home patches still point the session listing at its home.
    """
    del monkeypatch
    reaper = PtyReaper(tmp_path)
    yield reaper
    reaper.reap()
