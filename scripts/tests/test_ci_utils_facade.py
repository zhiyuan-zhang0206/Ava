"""External status imports and executable CLI path remain stable."""

import subprocess
import sys

from scripts import ci_utils
from scripts.ci import commands, status


def test_public_ci_symbols_keep_their_single_owner() -> None:
    assert ci_utils.CIStatus is status.CIStatus
    assert ci_utils.CIResult is status.CIResult
    assert ci_utils.check_ci is status.check_ci
    assert ci_utils.main is commands.main


def test_executable_cli_help_excludes_the_removed_override() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository CLI, help only
        [sys.executable, ci_utils.__file__, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--wait" in result.stdout
    assert "--merge" in result.stdout
    assert "--force" not in result.stdout
