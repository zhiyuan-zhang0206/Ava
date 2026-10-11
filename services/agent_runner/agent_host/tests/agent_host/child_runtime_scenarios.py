"""Fresh-process scenarios for the agent host's runtime tests.

Usage: `child_runtime_scenarios.py <scenario> [args]`.

- `rotate|noop|open_failure <log path>`: stdout transcript rotation; the parent
  points the child's fd 1 at the transcript so `fstat(1)` and the `dup2`
  re-point run against a genuine fd 1 rather than a pytest capture wrapper.
- `shutdown <failure> <home>`: one `_exercise_shutdown` failure scenario.
- `resource_join`: the unfinished-resource hard-exit scenario.

Each scenario lives beside its test; this script only gives it a clean
interpreter with static imports.
"""

import os
import sys
from pathlib import Path

from services.agent_runner.agent_host import stdout_log
from services.agent_runner.agent_host.tests.runtime.test_agent_host_shutdown import (
    _exercise_shutdown,
)
from services.agent_runner.agent_host.tests.runtime.test_resource_join import (
    _exercise_unfinished_main,
)


def _rotate_and_continue(log: Path) -> None:
    """Write past the ceiling, rotate, keep writing."""
    stdout_log._stdout_log_path = lambda: log
    stdout_log._STDOUT_LOG_ROTATE_BYTES = 1 << 20  # 1 MiB ceiling for the test
    os.write(1, b"x" * (1 << 20))
    rotated = stdout_log._rotate_stdout_log_if_needed()
    assert rotated == (1 << 20), f"expected rotation at ceiling, got {rotated}"
    os.write(1, b"after-rotation")
    # Stream objects (the loguru console sink writes through the same path) must
    # also land in the re-pointed file, not the renamed chunk.
    sys.stdout.write("via-stdout")
    sys.stdout.flush()
    sys.stderr.write("via-stderr")
    sys.stderr.flush()


def _noop_below_ceiling(log: Path) -> None:
    stdout_log._stdout_log_path = lambda: log
    stdout_log._STDOUT_LOG_ROTATE_BYTES = 1 << 30  # far above the few bytes written
    rotated = stdout_log._rotate_stdout_log_if_needed()
    assert rotated is None, f"expected no rotation, got {rotated}"
    os.write(1, b"tail")


def _open_failure_then_self_heal(log: Path) -> None:
    """First rotation attempt fails at the open (disk full / EMFILE): the
    transcript must be restored under the original name, and the next attempt
    must succeed — a stranded `.1` would grow past every bound."""
    stdout_log._stdout_log_path = lambda: log
    stdout_log._STDOUT_LOG_ROTATE_BYTES = 1 << 20  # 1 MiB ceiling for the test
    os.write(1, b"x" * (1 << 20))

    real_open = os.open

    def boom(_path: str, _flags: int, _mode: int = 0o777) -> int:
        raise OSError(28, "No space left on device")

    os.open = boom
    rotated = stdout_log._rotate_stdout_log_if_needed()
    assert rotated is None, f"expected failure, got {rotated}"
    os.open = real_open
    assert log.exists(), "transcript must be restored under its original name"
    assert not Path(str(log) + ".1").exists(), "no chunk may be stranded"
    os.write(1, b"after-open-failure")
    rotated = stdout_log._rotate_stdout_log_if_needed()
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
        _exercise_shutdown(sys.argv[2], Path(sys.argv[3]))
    elif scenario == "resource_join":
        _exercise_unfinished_main()
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")
