"""The real spawn helper detaches to an ancestor subreaper, not always PID 1."""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child-subreaper contract")
def test_caller_exit_preserves_child_adopted_by_ancestor_subreaper(
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    result = subprocess.run(  # noqa: S603 — isolated checked-in test probe, no shell
        [
            sys.executable,
            str(Path(__file__).with_name("subreaper_probe.py")),
            "ancestor",
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
        timeout=25,
    )
    evidence = json.loads(result.stdout)
    record_property("controlled_subreaper", json.dumps(evidence))
    assert evidence["caller_exited"] and evidence["child_live"]
    assert evidence["adopter"] != evidence["caller"]
    assert evidence["old_init_predicate"] is False
