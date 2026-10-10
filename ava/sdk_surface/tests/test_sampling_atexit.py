"""Ordinary Python shutdown reports refresh failures without an exit-code guarantee."""

import os
import subprocess
import sys


def test_installation_atexit_reports_failure_without_promising_a_nonzero_exit() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import ava
from ava.sdk_surface import install
from base.agents.sdk import call_policy
from base.packages.plugins.extensions import ExtensionRegistry
install.install(ExtensionRegistry(()))
original = ValueError('ordinary SDK refresh defect')
def read():
    raise original
call_policy._read_policy = read
installation = install.installed()
assert installation is not None
owner = installation.sampling
try:
    owner.read()
except ValueError as observed:
    assert observed is original
assert owner.worker is not None and owner.worker.completed.wait(2)
assert owner.error[0] is original
""",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Exception ignored in atexit callback" in result.stderr
    assert "ValueError: ordinary SDK refresh defect" in result.stderr
