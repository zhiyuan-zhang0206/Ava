"""Real runner exits and externally observed child ownership boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

import psutil
import psycopg
import pytest

from base.config import settings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="gateway runner is POSIX-only")


def _await_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        assert time.monotonic() < deadline, f"runner did not create {path}"
        time.sleep(0.02)


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except psutil.NoSuchProcess:
        return False


@contextmanager
def _processes(root: Path) -> Generator[list[psutil.Process], None, None]:
    """Own every test process, including deliberately exempt double-fork daemons."""
    tracked: list[psutil.Process] = []
    try:
        yield tracked
    finally:
        # PID files also collect children if setup failed before the readiness barrier.
        for path in root.glob("*.pid"):
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(int(path.read_text()))
                if str(root) in " ".join(process.cmdline()):
                    tracked.append(process)
        for process in reversed(tracked):
            with suppress(psutil.NoSuchProcess):
                process.kill()
        psutil.wait_procs(tracked, timeout=3)


def _scripts(root: Path, *, complete: bool) -> str:
    worker = root / "worker.py"
    worker.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "root, name = Path(sys.argv[1]), sys.argv[2]\n"
        "def term(*_):\n"
        "    (root / (name + '.term')).touch()\n"
        "    if name == 'child':\n"
        "        raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "if name == 'child':\n"
        "    child = subprocess.Popen([sys.executable, __file__, str(root), 'grandchild'])\n"
        "elif name == 'daemon':\n"
        "    if os.fork():\n"
        "        raise SystemExit(0)\n"
        "    os.setsid()\n"
        "    if os.fork():\n"
        "        raise SystemExit(0)\n"
        "(root / (name + '.tmp')).write_text(str(os.getpid()))\n"
        "(root / (name + '.tmp')).replace(root / (name + '.pid'))\n"
        "if name == 'child' and (root / 'reparent-on-record').exists():\n"
        "    while not (root / 'record-started').exists():\n"
        "        time.sleep(0.01)\n"
        "    raise SystemExit(0)\n"
        "time.sleep(60)\n"
    )
    return (
        "import os, socket, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"root = Path({str(root)!r})\n"
        f"worker = {str(worker)!r}\n"
        # Retain both live Popen objects: subprocess._active cannot discover these.
        "child = subprocess.Popen([sys.executable, worker, str(root), 'child'])\n"
        "session_child = subprocess.Popen(\n"
        "    [sys.executable, worker, str(root), 'session'], start_new_session=True)\n"
        "subprocess.run([sys.executable, worker, str(root), 'daemon'], check=True, timeout=5)\n"
        "while not (root / 'release').exists():\n"
        "    time.sleep(0.02)\n"
        # Both sleep and child wait outlast the stall budget, then leave a stable stall.
        "time.sleep(0.4)\n"
        "try:\n"
        "    child.communicate(timeout=0.4)\n"
        "except subprocess.TimeoutExpired:\n"
        "    pass\n"
        "(root / 'park-finished').touch()\n"
        + ("" if complete else "reader, writer = socket.socketpair()\nreader.recv(1)\n")
    )


def _capture_children(root: Path, runner_pid: int) -> list[psutil.Process]:
    owned: list[psutil.Process] = []
    for name in ("child", "grandchild", "session", "daemon"):
        path = root / f"{name}.pid"
        _await_file(path)
        owned.append(psutil.Process(int(path.read_text())))
    assert os.getsid(owned[2].pid) == owned[2].pid
    # Wait until the double-forked process really escaped the ancestry walk.
    deadline = time.monotonic() + 5
    while runner_pid in [parent.pid for parent in owned[3].parents()]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return owned


def _assert_cleanup(root: Path, owned: list[psutil.Process], *, complete: bool) -> None:
    child, grandchild, session_child, daemon = owned
    assert _alive(daemon), "pre-capture reparented daemon must remain exempt"
    if complete:
        assert all(_alive(process) for process in (child, grandchild, session_child))
        assert not list(root.glob("*.term")), "clean completion must not run stall cleanup"
    else:
        assert not _alive(child), "owned live-held Popen survived the runner hard exit"
        assert not _alive(grandchild), "TERM-resistant descendant survived escalation"
        assert not _alive(session_child), "setsid child escaped owned-child cleanup"
        assert all((root / f"{name}.term").exists() for name in ("child", "grandchild", "session"))


@pytest.mark.parametrize(
    "mode", ["stall", "complete", "missing-module-files", "delayed-record", "delayed-completion"]
)
def test_runner_hard_exit_child_ownership(
    db_conn: psycopg.Connection, unit_home: Path, mode: str
) -> None:
    # The runner is a separate process that boots its config from its home's `.env`
    # (`AVA_HOME` is this test's `unit_home`, which starts bare): name the test
    # database there, as a started cluster's `.env` does.
    (unit_home / ".env").write_text(f"AVA_DB_URL={settings.data_plane.db_url}\n")
    root = unit_home / "cleanup"
    root.mkdir()
    complete = mode in ("complete", "delayed-completion")
    script = _scripts(root, complete=complete)
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('cleanup', %s, 'python schedule.py', true, 'stopped') RETURNING id",
        (script,),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    setup = ""
    if mode == "missing-module-files":
        setup = "import subprocess, selectors; del subprocess.__file__; del selectors.__file__; "
    elif mode == "delayed-record":
        (root / "reparent-on-record").touch()
        setup = (
            "from gateway.schedules import runner as r\n"
            "from pathlib import Path\n"
            "import os, psutil, time\n"
            f"root = Path({str(root)!r})\n"
            "record_error = r._record_error\n"
            "def delayed_record(*args):\n"
            "    (root / 'record-started').touch()\n"
            "    deadline = time.monotonic() + 5\n"
            "    try:\n"
            "        grandchild = psutil.Process(int((root / 'grandchild.pid').read_text()))\n"
            "        while os.getpid() in [p.pid for p in grandchild.parents()]:\n"
            "            assert time.monotonic() < deadline\n"
            "            time.sleep(0.01)\n"
            "    except psutil.NoSuchProcess:\n"
            "        pass\n"
            "    record_error(*args)\n"
            "r._record_error = delayed_record\n"
        )
    elif mode == "delayed-completion":
        setup = (
            "from gateway.schedules import runner as r\n"
            "import threading, time\n"
            "finish = r._finish_completed\n"
            "def delayed_completion(*args):\n"
            "    assert not any(t.name.endswith('-stall-guard') for t in threading.enumerate())\n"
            "    time.sleep(0.4)\n"
            "    finish(*args)\n"
            "r._finish_completed = delayed_completion\n"
        )
    code = (
        setup + "from gateway.schedules import runner as r; import ava; "
        "ava.ensure_plugins_loaded = lambda: None; "
        "from base.config import settings; "
        "settings.gateway.schedule_stall_timeout_seconds = 0.15; "
        "settings.gateway.schedule_stall_check_interval_seconds = 0.02; "
        f"raise SystemExit(r.run({row[0]}))"
    )
    with _processes(root) as tracked, (root / "runner.log").open("w") as log:
        # Use a test-owned group so a buggy group kill fails without harming pytest.
        # The sibling models a PTY shell's other job in the runner's group.
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
        )
        tracked.append(psutil.Process(sibling.pid))
        runner = subprocess.Popen(  # noqa: S603 - fixed test harness and fixture schedule id
            [sys.executable, "-c", code], stdout=log, stderr=log, process_group=sibling.pid
        )
        tracked.append(psutil.Process(runner.pid))
        owned = _capture_children(root, runner.pid)
        tracked.extend(owned)
        assert os.getpgid(runner.pid) == os.getpgid(sibling.pid)
        (root / "release").touch()
        assert runner.wait(timeout=15) == (0 if complete else 1), (root / "runner.log").read_text()
        assert (root / "park-finished").exists(), "guard killed a legitimate park"
        if mode == "delayed-record":
            assert (root / "record-started").exists()
        assert sibling.poll() is None, "cleanup signalled an unrelated group member"
        _assert_cleanup(root, owned, complete=complete)
    run_row = db_conn.execute(
        "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s", (row[0],)
    ).fetchone()
    assert run_row == ((True, None) if complete else (False, "stalled (0s)"))


@pytest.mark.parametrize("blocked_write", ["error", "run", "complete"])
def test_stall_exit_bounds_failure_records(tmp_path: Path, blocked_write: str) -> None:
    """A blocked DB write cannot keep a stalled runner alive past the record budget."""
    code = (
        "from gateway.schedules import runner as r\n"
        "from pathlib import Path\n"
        "import threading, time\n"
        f"root = Path({str(tmp_path)!r})\n"
        "r.settings.gateway.schedule_stall_exit_record_deadline_seconds = 1.0\n"
        "def record_error(*args):\n"
        "    (root / 'started').write_text(str(time.monotonic()))\n"
        f"    time.sleep({60 if blocked_write == 'error' else 0.7})\n"
        "def record_run_end(*args, **kwargs):\n"
        "    (root / 'second-write').touch()\n"
        "    threading.Event().wait()\n"
        "r._record_error = record_error\n"
        "r._record_run_end = record_run_end\n"
        "r._stall_action(None, 1, 'test stall', 1)\n"
    )
    if blocked_write == "complete":
        code = _completion_during_stall_source(tmp_path)
    with subprocess.Popen([sys.executable, "-c", code]) as runner:  # noqa: S603 - fixed test source
        try:
            _await_file(tmp_path / "started")
            assert runner.wait(timeout=2) == 1
            elapsed = time.monotonic() - float((tmp_path / "started").read_text())
            assert 0.9 <= elapsed < 1.5, f"record deadline took {elapsed:.2f}s"
            assert (tmp_path / "second-write").exists() == (blocked_write == "run")
            if blocked_write == "complete":
                assert (tmp_path / "script-returned").exists()
                assert not (tmp_path / "completed").exists()
        finally:
            runner.kill()


def _completion_during_stall_source(root: Path) -> str:
    """The script returns while a real stall recorder is blocked; hard exit still wins."""
    return (
        "from gateway.schedules import runner as r\n"
        "from pathlib import Path\n"
        "import ava, runpy, time\n"
        f"root = Path({str(root)!r})\n"
        "r.settings.gateway.schedule_stall_check_interval_seconds = 0.01\n"
        "r.settings.gateway.schedule_stall_timeout_seconds = 0.01\n"
        "r.settings.gateway.schedule_stall_exit_record_deadline_seconds = 1.0\n"
        "r._schedule_dir = lambda _id: root\n"
        "r._load = lambda *_args: ('pass', 'python script.py')\n"
        "r._bind_schedule_actor = lambda *_args: None\n"
        "r._record_run_start = lambda *_args: 1\n"
        "r.base.host.proc.kill_process_tree = lambda *_args, **_kwargs: None\n"
        "r._StallGuard._sample = lambda _self: ('script.py', 1, 'blocked')\n"
        "ava.ensure_plugins_loaded = lambda: None\n"
        "def record_error(*_args):\n"
        "    (root / 'started').write_text(str(time.monotonic()))\n"
        "    time.sleep(60)\n"
        "def script(*_args, **_kwargs):\n"
        "    while not (root / 'started').exists():\n"
        "        time.sleep(0.005)\n"
        "    (root / 'script-returned').touch()\n"
        "r._record_error = record_error\n"
        "runpy.run_path = script\n"
        "r._finish_completed = lambda *_args: (root / 'completed').touch()\n"
        "raise SystemExit(r._run(None, 1))\n"
    )
