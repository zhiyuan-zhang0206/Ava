"""Runtime evidence value types remain independent from rollout controllers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHILD = Path(__file__).with_name("_evidence_import_child.py")


def test_runtime_consumers_do_not_load_retired_rollout_authority() -> None:
    repo = Path(__file__).resolve().parents[3]
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed child script and captured repo.
        [sys.executable, "-I", "-B", str(CHILD), str(repo)], check=True
    )
