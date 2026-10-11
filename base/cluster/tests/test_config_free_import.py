"""`import base.cluster` stays free of Settings and the home.

The restricted restore worker imports the cluster package before either exists.
The typed MachineIdentity value owner stays cold; its ConfigBoot factory does not
enter until the real composition root requests it.
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

assert "base.cluster.machine" in sys.modules
loaded = [n for n in ("base.config", "base.paths", "base.cluster.machine.boot") if n in sys.modules]
assert not loaded, loaded
"""


def test_cluster_package_modules_do_not_load_settings_or_the_home() -> None:
    repo = Path(__file__).resolve().parents[3]
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed code and captured repo.
        [sys.executable, "-I", "-B", "-c", _CODE, str(repo)], check=True
    )
