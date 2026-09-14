"""One isolated helper -> root -> unit process chain for the guard test.

Spawned by tests/test_helperproc.py. The helper writes the marker a signed
helper stamps into its direct child — its own pid, or
`AVA_TEST_HELPER_MARKER_VALUE` when the test needs a pid that is *not* on the
chain — then spawns a root, which spawns a unit. The unit reads the marker
file, sets the env var, and prints `parent_chain_intact()`'s verdict. The
marker is set by the unit rather than inherited only because the helper pid
exists after spawn; the ancestor walk the guard performs runs against the
real process table.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_MARKER_OVERRIDE_ENV = "AVA_TEST_HELPER_MARKER_VALUE"


def _run_next(next_role: str, marker_path: str) -> None:
    result = subprocess.run(  # noqa: S603 — fixed self-invocation, isolated test chain
        [sys.executable, __file__, next_role, marker_path],
        capture_output=True,
        text=True,
        check=True,
    )
    sys.stdout.write(result.stdout)


def main() -> None:
    role, marker_path = sys.argv[1], sys.argv[2]
    if role == "helper":
        value = os.environ.get(_MARKER_OVERRIDE_ENV, str(os.getpid()))
        Path(marker_path).write_text(value)
        _run_next("root", marker_path)
    elif role == "root":
        _run_next("unit", marker_path)
    else:
        os.environ["AVA_PERMISSIONS_HELPER_PID"] = Path(marker_path).read_text()
        from shared.helper_chain_guard import parent_chain_intact

        sys.stdout.write("intact" if parent_chain_intact() else "broken")


if __name__ == "__main__":
    main()
