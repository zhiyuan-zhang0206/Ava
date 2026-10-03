"""Tests for base/deploy/git/worktree_guard.py — the `git worktree remove` guard (issue #194)."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from base.sessions.pty import protocol
from base.sessions.pty.paths import fallback_dir, service_socket_path


class _FakeService:
    """A stand-in pty-sessions service: answers every request with a fixed session list.

    Bound at the path the guard computes for the home's run directory, before the
    guard is run, so the home is already in the state the guard must leave it in.
    """

    def __init__(self, path: Path, sessions: list[dict[str, Any]]) -> None:
        if path.parent == fallback_dir():
            path.parent.mkdir(mode=0o700, exist_ok=True)
        path.unlink(missing_ok=True)
        self._path = path
        self._sessions = sessions
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                line = protocol.read_line(conn)
                if line is None:
                    continue
                request = protocol.decode_object(line)
                conn.sendall(
                    protocol.encode(protocol.ok(request["id"], {"sessions": self._sessions}))
                )

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)
        with contextlib.suppress(OSError):
            self._path.unlink()


def _run_guard(target: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the real guard script the way cleanup does — from inside the
    target, through a transient shell (the #3685 habit)."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_worktree_remove.py"
    return subprocess.run(  # noqa: S603 — test-owned interpreter, fixture path, no untrusted input
        ["/bin/sh", "-c", f'cd "{target}" && "{sys.executable}" "{script}" "{target}"'],
        capture_output=True,
        text=True,
        check=False,
    )


def test_invocation_shell_inside_target_does_not_self_refuse(tmp_path: Path) -> None:
    """#3685: `cd <worktree> && check` puts the invoking shell's cwd inside the
    target; that chain is the remover itself, not a live anchor. The false
    REFUSE it produced pushed callers toward `--force` — how a real anchor gets
    missed."""
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    result = _run_guard(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("OK")


def test_pipeline_sibling_consumer_is_not_an_anchor(tmp_path: Path) -> None:
    """#3685 QA follow-up: cleanup often pipes the guard's output — `cd T &&
    check T 2>&1 | tail`. The consumer (`tail` / `cat`) is the caller's
    SIBLING, not an ancestor; it shares the invocation's process group and
    must not read as an anchor. `rc=` echoes the guard's own status through
    the pipe."""
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_worktree_remove.py"
    result = subprocess.run(  # noqa: S603 — test-owned interpreter, fixture path, no untrusted input
        [
            "/bin/sh",
            "-c",
            f'cd "{target}" && {{ "{sys.executable}" "{script}" "{target}"; echo "rc=$?"; }} 2>&1 | cat',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    assert f"OK {target}" in result.stdout, result.stdout + result.stderr


def test_true_anchor_still_refuses_from_inside_invocation(tmp_path: Path) -> None:
    """The invoking-chain exclusion must not weaken the guard: an unrelated
    process genuinely anchored in the target still refuses."""
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=target, start_new_session=True
    )
    try:
        time.sleep(0.5)  # let psutil observe the sleeper's cwd
        result = _run_guard(target)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "REFUSE" in result.stdout and str(target) in result.stdout
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_python_without_psutil_gets_its_own_exit_code_and_says_what_to_run(
    tmp_path: Path,
) -> None:
    """A system python lacks psutil. That is no verdict: exit 1 reads as REFUSE, which
    is what the crash used to look like."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_worktree_remove.py"
    launch = (
        "import runpy, sys; sys.modules['psutil'] = None; "
        "sys.argv = [sys.argv[1], sys.argv[2]]; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    result = subprocess.run(  # noqa: S603 - test-owned interpreter and fixture path
        [sys.executable, "-c", launch, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout == ""
    assert "MISSING DEPENDENCY psutil" in result.stderr
    assert ".venv/bin/python scripts/check_worktree_remove.py" in result.stderr


# Runs the real script with every outbound channel replaced by a recorder that
# raises, and reports what was touched. Everything the scan could reach a cluster
# through: sockets, HTTP clients, urllib, the database drivers, redis. A connect to a
# unix-socket path is this machine's own pty-sessions service, the one thing the scan
# is meant to ask, so it passes through; any other address is a dial.
_NO_DIAL_DRIVER = r"""
import json, runpy, socket, sys, urllib.request

dialed = []


def refuse(name):
    def _refused(*args, **kwargs):
        dialed.append(name)
        raise RuntimeError(name + " was called")

    return _refused


def local_only(dotted, original):
    def _guarded(self, address, *args, **kwargs):
        if isinstance(address, str):
            return original(self, address, *args, **kwargs)
        dialed.append(dotted)
        raise RuntimeError(dotted + " was called")

    return _guarded


def patch(dotted):
    path, attr = dotted.rsplit(".", 1)
    try:
        obj = __import__(path.split(".")[0])
        for part in path.split(".")[1:]:
            obj = getattr(obj, part)
    except (ImportError, AttributeError):
        return
    if dotted in ("socket.socket.connect", "socket.socket.connect_ex"):
        setattr(obj, attr, local_only(dotted, getattr(obj, attr)))
    else:
        setattr(obj, attr, refuse(dotted))


for dotted in (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.create_connection",
    "urllib.request.urlopen",
    "httpx.Client.send",
    "httpx.AsyncClient.send",
    "requests.Session.send",
    "psycopg.connect",
    "psycopg.Connection.connect",
    "psycopg_pool.ConnectionPool.__init__",
    "redis.connection.Connection.connect",
):
    patch(dotted)

script, target = sys.argv[1:3]
sys.argv = [script, target]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exit_:
    code = exit_.code
print("RESULT:" + json.dumps({"code": code, "dialed": dialed}))
"""


def _tree_state(home: Path) -> list[tuple[str, int, int, int]]:
    """Every entry under `home` (and `home` itself) with its mode, mtime and size."""
    entries = [home, *sorted(home.rglob("*"))]
    return [
        (
            str(entry.relative_to(home)),
            entry.stat().st_mode,
            entry.stat().st_mtime_ns,
            entry.stat().st_size,
        )
        for entry in entries
    ]


def test_the_guard_reads_the_real_home_and_dials_and_writes_nothing(tmp_path: Path) -> None:
    """The guard finds a live session anchor by asking the home's own pty-sessions
    service, so it must read the real home (no scratch home): the service is the only
    place a PTY shell's cwd is written down. It is also the one tool a developer runs
    straight from a checkout on a host that runs a cluster, so it must stay read-only:
    no gateway or database dial, and nothing created, repaired or chmodded under the
    home. The home here is deliberately not owner-only and holds an unreadable `.env`
    naming a database; any attempt to open the home as the owner's (`ensure_private_dir`)
    or to boot the cluster config from it changes or breaks something."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_worktree_remove.py"
    home = tmp_path / "ava-home"
    (home / "run").mkdir(parents=True)
    target = tmp_path / "worktrees" / "wt-under-test"
    target.mkdir(parents=True)
    service = _FakeService(
        service_socket_path(home / "run"),
        [
            {
                "name": "1",
                "pid": 4242,
                "create_time": 0.0,
                "starttime": None,
                "cmd": "/bin/bash -l -i",
                "cwd": str(target),
                "started_at": 0.0,
                "generation": None,
            }
        ],
    )
    env_file = home / ".env"
    env_file.write_text("AVA_DB_URL=postgresql://ava@127.0.0.1:1/ava\nAVA_CLUSTER_SECRET=canary\n")
    env_file.chmod(0)
    home.chmod(0o755)
    before = _tree_state(home)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env.update(AVA_HOME=str(home), HOME=str(tmp_path / "user"))
    try:
        result = subprocess.run(  # noqa: S603 - test-owned interpreter and fixture paths
            [sys.executable, "-c", _NO_DIAL_DRIVER, str(script), str(target)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        after = _tree_state(home)
    finally:
        env_file.chmod(0o600)
        service.close()
    assert result.returncode == 0, result.stdout + result.stderr
    [verdict] = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT:")]
    assert json.loads(verdict.removeprefix("RESULT:")) == {"code": 1, "dialed": []}
    assert "pty session '1'" in result.stdout  # the real home's service was asked
    assert after == before
