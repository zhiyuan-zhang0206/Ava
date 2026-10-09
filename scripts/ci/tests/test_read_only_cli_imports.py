"""Read-only CLI startup excludes owner-only operations in a fresh interpreter."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("arguments", [["42"], ["42", "--wait"]])
def test_read_only_cli_does_not_load_owner_operations(arguments: list[str]) -> None:
    code = """
import importlib.abc, json, os, sys
sys.path.insert(0, sys.argv[1])
class RejectOwnerImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in ('scripts.ci.pull_requests.owner_operations', 'scripts.ci.pull_requests.trunk_api'):
            raise AssertionError('read-only CLI imported owner operations: ' + fullname)
sys.meta_path.insert(0, RejectOwnerImports())
os.environ.pop('TRUNK_API_TOKEN', None)
os.environ['CI_QUEUE'] = 'unrelated-owner-queue'
from scripts.ci import cli as ci_utils
from scripts.ci.pull_requests import status
status.check_ci = lambda *a, **k: status.CIResult(status.CIStatus.ALL_PASSED)
raise SystemExit(ci_utils.main(json.loads(sys.argv[2])))
"""
    result = subprocess.run(  # noqa: S603 - hermetic code built from fixed test inputs
        [sys.executable, "-I", "-c", code, str(Path.cwd()), json.dumps(arguments)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "green" in result.stdout
