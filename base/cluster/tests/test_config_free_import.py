"""`import base.cluster` stays free of Settings and the home.

The restricted restore worker imports the cluster package before either exists, so
anything the package reads from them is imported inside the function that needs it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CODE = """
import sys

sys.path.insert(0, sys.argv[1])
import base.cluster
import base.cluster.ownership
import base.cluster.port_preflight

loaded = [n for n in ("base.config", "base.paths", "base.cluster.machine") if n in sys.modules]
assert not loaded, loaded
"""


def test_cluster_package_modules_do_not_load_settings_or_the_home() -> None:
    repo = Path(__file__).resolve().parents[3]
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed code and captured repo.
        [sys.executable, "-I", "-B", "-c", _CODE, str(repo)], check=True
    )
