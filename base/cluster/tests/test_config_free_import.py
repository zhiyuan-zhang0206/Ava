"""`import base.cluster` stays free of Settings and the home.

The restricted restore worker imports the cluster package before either exists.
The typed MachineIdentity value owner stays cold; its ConfigBoot factory does not
enter until the real composition root requests it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CHILD = Path(__file__).resolve().parents[1] / "machine" / "tests" / "_import_child.py"


def test_cluster_package_modules_do_not_load_settings_or_the_home() -> None:
    repo = Path(__file__).resolve().parents[3]
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed child script and captured repo.
        [sys.executable, "-I", "-B", str(_CHILD), "cluster_package_modules", str(repo)], check=True
    )
