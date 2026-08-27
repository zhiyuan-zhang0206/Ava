"""shared.proc — the liveness probe and `run_bounded`'s tree-bounded timeout.

The `run_bounded` cases spawn REAL process trees (parent + grandchild), because
the defect being fixed lives entirely in who survives a kill: a mocked
subprocess cannot leak a descendant.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from shared.paths import run_dir
from shared.platform import IS_LINUX
from shared.proc import (
    hosting_supervised_session,
    kill_process_tree,
    process_alive,
    run_bounded,
)
from shared.proc import (
    timeout_stderr_tail as proc_timeout_stderr_tail,
)
from shared.session_record import SessionRecord, pid_starttime_ticks


def test_own_pid_is_alive() -> None:
    """This very process is, by definition, alive."""
    assert process_alive(os.getpid()) is True


def test_unused_pid_is_dead() -> None:
    """A pid that names no process reads as dead (ProcessLookupError)."""
    # Very high pid that is essentially never allocated; if it somehow is, the
    # probe still returns a bool — the assertion below just checks the typical
    # "no such process" path.
    assert process_alive(2_000_000_000) is False


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_hosting_supervised_session_uses_starttime_despite_wall_clock_drift(
    unit_home: Path,
) -> None:
    """Update lineage recognizes a matching stable tick even when btime drifted."""
    starttime = pid_starttime_ticks(os.getpid())
    assert starttime is not None
    name = "ava-test-hosting-starttime"
    path = run_dir() / "sessions" / f"{name}.json"
    SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=starttime,
    ).write(path)
    try:
        assert hosting_supervised_session() == name
    finally:
        path.unlink(missing_ok=True)


# --- run_bounded fixtures -------------------------------------------------
#
# The fixture reproduces the shape measured on the Windows agent-runner: the
# process Python spawns is a *launcher* that starts the real work and then goes
# to sleep. Killing only the top process leaves the work running.

_PARENT_SRC = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(300)
"""

# A parent whose grandchild exits on its own and is never reaped: the grandchild
# is a zombie child by the time the timeout enumerates the tree, which is the
# "descendant went away before the kill" case in its deterministic form.
_PARENT_SHORTLIVED_CHILD_SRC = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", ""])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(300)
"""

_TIMEOUT_S = 3.0


def _fixture(tmp_path: Path, src: str) -> tuple[list[str], Path]:
    """Write a parent script; return its argv and the path it reports its
    grandchild's pid to."""
    script = tmp_path / "parent.py"
    script.write_text(src)
    pid_file = tmp_path / "grandchild.pid"
    return [sys.executable, str(script), str(pid_file)], pid_file


def _grandchild_pid(pid_file: Path) -> int:
    """The grandchild pid the fixture recorded before it went to sleep. Written
    within milliseconds of launch, so a timeout of seconds always sees it."""
    assert pid_file.exists(), "fixture never reported its grandchild pid"
    return int(pid_file.read_text())


def _dead(pid: int) -> bool:
    """Whether `pid` is gone. A zombie counts as dead: a killed grandchild whose
    parent we also killed is unreaped for the instant before init collects it."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _dead(pid):
            return True
        time.sleep(0.05)
    return False


# --- the delta this PR ships ---------------------------------------------


def test_plain_subprocess_run_leaks_the_grandchild(tmp_path: Path) -> None:
    """The pre-fix behaviour, asserted rather than assumed: `subprocess.run`'s
    timeout kills the process it spawned and the grandchild survives.

    This is the leak. If a future CPython ever tree-kills, this test fails — and
    that failure is the news, not a break.
    """
    argv, pid_file = _fixture(tmp_path, _PARENT_SRC)
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(argv, timeout=_TIMEOUT_S, check=False)  # noqa: S603 — fixture argv
    gpid = _grandchild_pid(pid_file)
    try:
        assert not _dead(gpid), "expected the stdlib timeout to leave the grandchild alive"
    finally:
        kill_process_tree(gpid)


def test_run_bounded_kills_the_whole_tree(tmp_path: Path) -> None:
    """`run_bounded` on the same fixture: parent AND grandchild are gone, and
    `TimeoutExpired` still propagates so a caller that treats a timeout as a
    fetch failure is unaffected."""
    argv, pid_file = _fixture(tmp_path, _PARENT_SRC)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_bounded(argv, timeout=_TIMEOUT_S, capture_output=True, text=True)
    gpid = _grandchild_pid(pid_file)
    try:
        assert _wait_dead(gpid), "the grandchild outlived the bound"
        assert caught.value.timeout == _TIMEOUT_S
    finally:
        kill_process_tree(gpid)  # no-op when the bound did its job


def test_run_bounded_tolerates_a_descendant_that_already_exited(tmp_path: Path) -> None:
    """A descendant gone before the kill is normal, not an error: the timeout
    still surfaces as `TimeoutExpired` and the tree still ends up dead."""
    argv, pid_file = _fixture(tmp_path, _PARENT_SHORTLIVED_CHILD_SRC)
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(argv, timeout=_TIMEOUT_S, capture_output=True, text=True)
    assert _wait_dead(_grandchild_pid(pid_file))


def test_kill_process_tree_on_an_absent_pid_is_a_noop() -> None:
    """Killing a pid that is already gone must not raise — every caller reaches
    this racing the process's own exit."""
    kill_process_tree(2_000_000_000)


# --- the subprocess.run-shaped surface -----------------------------------


def test_run_bounded_returns_a_completed_process() -> None:
    """The happy path is `subprocess.run`'s: returncode + captured output, and no
    exception on a non-zero exit."""
    result = run_bounded(
        [sys.executable, "-c", "import sys; print('out'); sys.exit(3)"],
        timeout=30.0,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out"


def test_run_bounded_rejects_check() -> None:
    """There is deliberately no `check=`; a caller assuming run's semantics gets
    a TypeError rather than a silently ignored kwarg."""
    with pytest.raises(TypeError):
        run_bounded([sys.executable, "-c", ""], timeout=30.0, check=True)


def test_run_bounded_rejects_capture_output_with_explicit_pipes() -> None:
    """`capture_output` must not silently overwrite a caller's own stdout."""
    with pytest.raises(ValueError, match="capture_output"):
        run_bounded(
            [sys.executable, "-c", ""],
            timeout=30.0,
            capture_output=True,
            stdout=subprocess.DEVNULL,
        )


# --- timeout_stderr_tail: the timeout-point evidence ----------------------


def test_timeout_stderr_tail_returns_the_last_lines_as_text() -> None:
    """The partial stderr a `run_bounded` timeout carries (drained after the
    tree kill) is normalized to the last non-empty lines, joined — the timeout
    point a caller logs or returns."""
    exc = subprocess.TimeoutExpired(
        cmd=["git", "fetch"],
        timeout=30.0,
        stderr="ssh: connect to host github.com port 22: Connection timed out\n",
    )
    assert proc_timeout_stderr_tail(exc) == (
        "ssh: connect to host github.com port 22: Connection timed out"
    )


def test_timeout_stderr_tail_normalizes_bytes_and_none() -> None:
    """`TimeoutExpired.stderr` is typed `str | bytes | None`; bytes decode and
    None (a child killed before it wrote anything) yields an empty string —
    itself the evidence that the hang was pre-output, not mid-transfer."""
    exc = subprocess.TimeoutExpired(
        cmd=["git", "fetch"], timeout=30.0, stderr=b"Receiving objects: 45% (123/271)\r"
    )
    assert proc_timeout_stderr_tail(exc) == "Receiving objects: 45% (123/271)"

    exc = subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=30.0, stderr=None)
    assert proc_timeout_stderr_tail(exc) == ""


def test_timeout_stderr_tail_bounds_the_number_of_lines() -> None:
    exc = subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=30.0, stderr="a\nb\nc\nd\n")
    assert proc_timeout_stderr_tail(exc) == "b | c | d"
    assert proc_timeout_stderr_tail(exc, lines=2) == "c | d"


# --- the git-driving modules stay converted ------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The modules that drive git on Ava's behalf — the ones whose timeouts were
# silently bounding a launcher stub on Windows. A `subprocess.run(timeout=)`
# reappearing in any of them is the regression, and it is invisible on review
# because it looks exactly like a correct bound.
_GIT_DRIVING_MODULES = (
    "ops/cluster_deploy.py",
    "ops/ops_cluster.py",
    "cli/commands/_update_git.py",
    "shared/cluster_drift.py",
)


@pytest.mark.parametrize("rel", _GIT_DRIVING_MODULES)
def test_git_driving_modules_do_not_bound_with_subprocess_run(rel: str) -> None:
    """No `subprocess.run(..., timeout=...)` in the modules that drive git: a
    timeout there must come from `run_bounded`, which bounds the tree."""
    tree = ast.parse((_REPO_ROOT / rel).read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "subprocess.run"
        and any(kw.arg == "timeout" for kw in node.keywords)
    ]
    assert not offenders, f"{rel}: use shared.proc.run_bounded at line(s) {offenders}"
