import os
import sys
from pathlib import Path

from services.agent_runner.agent_host import stdout_log
from services.agent_runner.agent_host.tests.runtime.shutdown.test_agent_host_shutdown import (
    exercise_shutdown,
)
from services.agent_runner.agent_host.tests.runtime.test_resource_join import (
    exercise_unfinished_main,
)


def _rotate_and_continue(log: Path) -> None:
    stdout_log.stdout_log_path = lambda: log
    stdout_log.STDOUT_LOG_ROTATE_BYTES = 1 << 20
    os.write(1, b"x" * (1 << 20))
    rotated = stdout_log.rotate_stdout_log_if_needed()
    assert rotated == (1 << 20), f"expected rotation at ceiling, got {rotated}"
    os.write(1, b"after-rotation")
    sys.stdout.write("via-stdout")
    sys.stdout.flush()
    sys.stderr.write("via-stderr")
    sys.stderr.flush()


def _noop_below_ceiling(log: Path) -> None:
    stdout_log.stdout_log_path = lambda: log
    stdout_log.STDOUT_LOG_ROTATE_BYTES = 1 << 30
    rotated = stdout_log.rotate_stdout_log_if_needed()
    assert rotated is None, f"expected no rotation, got {rotated}"
    os.write(1, b"tail")


def _open_failure_then_self_heal(log: Path) -> None:
    stdout_log.stdout_log_path = lambda: log
    stdout_log.STDOUT_LOG_ROTATE_BYTES = 1 << 20
    os.write(1, b"x" * (1 << 20))

    real_open = os.open

    def boom(_path: str, _flags: int, _mode: int = 0o777) -> int:
        raise OSError(28, "No space left on device")

    os.open = boom
    rotated = stdout_log.rotate_stdout_log_if_needed()
    assert rotated is None, f"expected failure, got {rotated}"
    os.open = real_open
    assert log.exists(), "transcript must be restored under its original name"
    assert not Path(str(log) + ".1").exists(), "no chunk may be stranded"
    os.write(1, b"after-open-failure")
    rotated = stdout_log.rotate_stdout_log_if_needed()
    assert rotated == (1 << 20) + len(b"after-open-failure"), f"self-heal failed: {rotated}"
    os.write(1, b"after-self-heal")


if __name__ == "__main__":
    scenario = sys.argv[1]
    if scenario == "rotate":
        _rotate_and_continue(Path(sys.argv[2]))
    elif scenario == "noop":
        _noop_below_ceiling(Path(sys.argv[2]))
    elif scenario == "open_failure":
        _open_failure_then_self_heal(Path(sys.argv[2]))
    elif scenario == "shutdown":
        exercise_shutdown(sys.argv[2], Path(sys.argv[3]))
    elif scenario == "resource_join":
        exercise_unfinished_main()
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")
