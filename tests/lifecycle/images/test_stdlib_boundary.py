"""The release store and runtime preparation work from a bare interpreter.

The filesystem contract (release-store.yml) and the checkout-retiring runtime
proof import these modules and run preparation tools without the project
environment. This guard also runs inside the full suite, where every package is
installed, so a third-party import anywhere in the chain — including one taken
lazily while a tool runs — fails before those workflows see it.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_PROBE = """
import importlib.abc, sys

class StdlibOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top in sys.stdlib_module_names or top == "shared":
            return None
        raise ModuleNotFoundError(f"non-stdlib import {name!r}", name=name)

sys.meta_path.insert(0, StdlibOnly())
sys.path.insert(0, sys.argv[1])
# runtime_plugins is imported lazily by preparation's plugin inventory.
import shared.runtime_plugins, shared.runtime_prepare, shared.runtime_release
"""

# A preparation tool whose child forks a grandchild only after the tool (the
# group leader) has exited: the late member races the closure.
_TOOL = """
import os, subprocess, sys
subprocess.Popen([sys.executable, "-I", "-c", {late!r}])
open("group", "w").write(str(os.getpgrp()))
print(os.getpgrp() == os.getpid())
"""
_LATE = "import os, time; time.sleep(0.2); os.fork(); time.sleep({linger})"
_RUN = """
import os, pathlib
from shared.runtime_prepare import _run
from shared.runtime_release import ReleaseRejectedError

work = pathlib.Path(sys.argv[2])
tool = [sys.executable, "-I", str(work / "tool.py")]

def closed() -> None:
    try:
        os.killpg(int((work / "group").read_text()), 0)
    except ProcessLookupError:
        return
    raise AssertionError("the preparation tool's group outlived it")

(work / "tool.py").write_text({finishing!r})
assert _run(tool, work).strip() == "True"  # natural completion waits for the late fork
closed()
(work / "tool.py").write_text({lingering!r})
try:
    _run(tool, work, timeout=1)
except ReleaseRejectedError as exc:
    assert "timed out" in str(exc), exc
else:
    raise AssertionError("a lingering group member did not fail the tool")
closed()
"""


def _probe(*, run: bool) -> str:
    if not run:
        return _PROBE
    return _PROBE + _RUN.format(
        finishing=_TOOL.format(late=_LATE.format(linger=0.1)),
        lingering=_TOOL.format(late=_LATE.format(linger=60)),
    )


class StdlibBoundaryTests(unittest.TestCase):
    def _bare(self, probe: str) -> None:
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run(  # noqa: S603 — this interpreter with a fixed probe
                [sys.executable, "-I", "-c", probe, str(_ROOT), work],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_store_and_preparation_import_only_the_standard_library(self) -> None:
        self._bare(_probe(run=False))

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group preparation")
    def test_preparation_tools_run_and_close_their_group_from_a_bare_interpreter(self) -> None:
        self._bare(_probe(run=True))


if __name__ == "__main__":
    unittest.main()
