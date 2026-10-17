"""The pre-update gate runs NEW's verify from a throwaway worktree, not the home's source checkout.

The 2026-10-04 prod dry run: the worktree process was refused the database login
(`NoDatabaseAuthorityError ... this process runs .../pre-update-verify, not the home's source
checkout`) and the sweep exited 2 with nothing checked. The gate now has the home's own checkout
dump the rows and hands them to NEW as a file; these run the real processes in that shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.paths import repo_root

_ENDPOINT = "postgresql://meiva@127.0.0.1:6433/meiva"

_CHILD = Path(__file__).with_name("child_schedules_verify.py")


@pytest.fixture
def worktree_process(
    tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> Callable[[str, Path], subprocess.CompletedProcess[str]]:
    """Run a child scenario as a process whose code root is not the home's recorded source checkout."""
    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    (home / ".env").write_text(f"MEIVA_DB_URL={_ENDPOINT}\n")
    intent = home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(home), "checkout": str(tmp_path / "source")}))
    intent.chmod(0o600)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "MEIVA_HOME": str(home)}
    env["MEIVA_CONFIG_FETCH"] = "skip"

    def run(scenario: str, arg: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_CHILD), scenario, str(arg)],
            cwd=repo_root(),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    return run


def test_a_process_outside_the_home_checkout_cannot_read_the_table(
    worktree_process: Callable[[str, Path], subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    proc = worktree_process("read_table", tmp_path / "rows.json")
    assert proc.returncode != 0
    assert "NoDatabaseAuthorityError" in proc.stderr
    assert "not the home's source checkout" in proc.stderr


def test_the_sweep_over_a_rows_file_needs_no_database_authority(
    worktree_process: Callable[[str, Path], subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([[1, "clean", "import os\n"], [2, "stopped-empty", ""]]))
    proc = worktree_process("check_rows", rows)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checked=2 green=2 red=0 rc=0" in proc.stdout


def test_the_sweep_over_a_rows_file_still_reports_a_red_row(
    worktree_process: Callable[[str, Path], subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([[7, "drifted", "import zz_meiva_verify_missing\n"]]))
    proc = worktree_process("check_rows", rows)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "RED id=7 name=drifted missing=zz_meiva_verify_missing" in proc.stdout
