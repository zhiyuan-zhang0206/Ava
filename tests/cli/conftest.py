"""Shared fixtures for the CLI tests.

Orchestration tests record the local pause/resume seam while updating the
actual host posture. Without that posture effect, a fake recovery would leave
later gateway requests behind a stale 503 gate. Production service and OS job
boundaries remain guarded by the root fixtures.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import psutil
import pytest

from shared import service_selection as ds
from shared.config import settings
from shared.native_process.ownership import OwnedProcess
from shared.paths import run_dir
from shared.session_backend import PtySessionBackend
from shared.session_record import SessionRecord


@pytest.fixture
def _installed_machine_identity(unit_home: pathlib.Path) -> Iterator[None]:
    """Give this test's unit home a machine identity, as a real install has.

    `unit_home` is deliberately a bare directory — several tests model a home
    that has never been through `ava start` and assert exactly that (see
    `test_start_arg_writes_to_file_for_persistence`, whose premise is "files
    also do not exist"). So the identity cannot be pre-written there.

    But an install that lands content into the CLUSTER registry records
    `local:<machine>` provenance for a local-path source, which needs a machine
    name — and a home doing `ava skill install` is by definition an installed
    unit, not a virgin one. Modules exercising the install paths opt in with
    `pytestmark = pytest.mark.usefixtures("_installed_machine_identity")`.
    """
    from shared.machine import reset_identity

    (unit_home / "machine_name").write_text("unit-test-machine", encoding="utf-8")
    reset_identity()
    yield
    reset_identity()


@pytest.fixture
def as_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[pathlib.Path], AbstractContextManager[pathlib.Path]]:
    """Run a block as the machine whose `$AVA_HOME` is the given directory.

    A "machine" in these tests IS its `$AVA_HOME`: `skills_dir()`, the
    `installed.json` install registry and `machine_name` all hang off it, so
    pointing `settings.general.ava_home` at a second directory gives a genuinely
    distinct machine to every path under test while the Postgres URL is
    untouched. Two homes, one PG.

    `reset_identity()` on BOTH edges is the load-bearing part, not the home
    swap: `machine_name()` caches, and a stale cache would let the second home
    claim to be the first — which would make a cross-machine test pass for the
    wrong reason, since rows record `local:<machine>`.

    Shared rather than copied because it is exactly the kind of helper whose
    subtle half (the cache reset) gets dropped in the copy.
    """
    from shared.machine import reset_identity

    @contextmanager
    def _enter(home: pathlib.Path) -> Generator[pathlib.Path]:
        home.mkdir(parents=True, exist_ok=True)
        (home / "machine_name").write_text(home.name, encoding="utf-8")
        with monkeypatch.context() as m:
            m.setattr(settings.general, "ava_home", home)
            reset_identity()
            try:
                yield home
            finally:
                reset_identity()

    return _enter


@pytest.fixture(autouse=True)
def _isolate_disabled_services_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Every CLI test's durable `--disable-service` marker lives in a per-test tmp
    file, never in the worker's shared session home.

    An operator `ava start --disable-service X` persists X to
    `$AVA_HOME/disabled_services` (`persist_services=True` — the real, intended
    behavior the start tests exercise). The suite's session home is shared by
    every test in a worker, so an un-isolated marker outlives the test that wrote
    it: a `cmd_start(disabled_services=("restarter",))` left "restarter durably
    disabled" behind, and a later `unpause_local_cluster` test in the same worker
    early-returned — neither respawning the restarter nor raising (CI #1172/#1173
    shard-5 flake, task #2177). Same redirection `tests/shared/test_disabled_services.py`
    uses: the marker is per-unit durable state, so each test gets a fresh one.
    """
    monkeypatch.setattr(ds, "selection_path", lambda: tmp_path / "service-selection.json")


@pytest.fixture(autouse=True)
def _isolate_local_pause_journal(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stop intentionally retains its hold; it must not hold the next test."""
    from shared import pause_owner

    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause-owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause-owner.lock")


@pytest.fixture(autouse=True)
def local_pauses(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Orchestration tests stub the native daemon boundary explicitly.

    Kernel integration tests call ops.agent_pause or the public command kernel
    directly; a phase-ordering test must not dial this host's real agent-host.
    """
    calls: list[bool] = []
    monkeypatch.setattr("ops.cluster_pause.pause_local_cluster", lambda: calls.append(True))
    return calls


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
