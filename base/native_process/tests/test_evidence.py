"""Runtime evidence value types remain independent from rollout controllers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_runtime_consumers_do_not_load_retired_rollout_authority() -> None:
    repo = Path(__file__).resolve().parents[3]
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "import base.native_process.evidence;"
        "loaded=[name for name in sys.modules if name.startswith("
        "('base.managed_writer','base.runtime_publication','cli.commands._update'))];"
        "assert not loaded, loaded"
    )
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed code and captured repo.
        [sys.executable, "-I", "-B", "-c", code, str(repo)], check=True
    )
