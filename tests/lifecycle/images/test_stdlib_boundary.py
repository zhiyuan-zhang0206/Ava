"""The release store and runtime preparation import from a bare interpreter.

The filesystem contract (release-store.yml) and the checkout-retiring runtime
proof run these modules without the project environment. This guard also runs
inside the full suite, where every package is installed, so a new third-party
import anywhere in their import chain fails before those workflows see it.
"""

import subprocess
import sys
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
import shared.runtime_prepare, shared.runtime_release
"""


class StdlibBoundaryTests(unittest.TestCase):
    def test_release_store_and_preparation_import_only_the_standard_library(self) -> None:
        result = subprocess.run(  # noqa: S603 — this interpreter with a fixed probe
            [sys.executable, "-I", "-c", _PROBE, str(_ROOT)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
