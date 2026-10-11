"""Child process for test_test_selector.py: print one duration estimate.

The hash-seed regression needs a fresh interpreter per `PYTHONHASHSEED`, so the
estimate is computed here and the test compares what each seed printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.test_selector import estimate_seconds

print(
    repr(
        estimate_seconds(
            {"tests/a.py", "tests/b.py", "tests/c.py"},
            {
                "tests/a.py::test_a": 1e16,
                "tests/b.py::test_b": 1.0,
                "tests/c.py::test_c": 1.0,
            },
        )
    )
)
