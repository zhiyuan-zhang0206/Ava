"""Machine value types and the pipeline type import without loading settings."""

import os
import subprocess
import sys
from pathlib import Path


def test_machine_value_and_pipeline_type_imports_do_not_load_settings(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)
    result = subprocess.run(  # noqa: S603 — fixed interpreter and child script, isolated temporary home
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_machine_import_child.py")),
            "machine_value_imports",
            str(Path(__file__).resolve().parents[3]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
