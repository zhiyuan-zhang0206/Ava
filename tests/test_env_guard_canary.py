"""The trimmed env guard still fires on a simulated e2e env leak.

Task #2446 (analysis #2411): the gated env-guard job now runs ONE representative
scenario-marked e2e test before ``tests/test_home_isolation.py`` instead of the
full ``tests/e2e/`` directory. The detectors' sensitivity depends on that warmup
shape — they only speak when an earlier test in the same process mutated and
(should have) restored the environment. This meta-test proves the property: a
session-scoped fixture that mutates ``AVA_CLUSTER_SECRET`` and never restores it
(the 2026-07-29 leak shape) must make the sentinel fail in the same process.

Green here = the trimmed guard catches the leak. Red here = the detectors can no
longer speak (sentinel blind spot), which means the guard would certify an
environment that is not protected — treat it as a blocking regression of the
guard, not as a flaky meta-test.

The inner run is a second pytest process collecting only the synthetic leak
package and the real ``tests/test_home_isolation.py``: no recursion, and the
assertions below check the failure is the cluster-secret sentinel itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LEAKED = "mutated-sentinel-value"


def _write_leak_package(tmp_path: Path) -> Path:
    """A package whose conftest leaks the operator secret for the whole session."""
    pkg = tmp_path / "leaky_e2e"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "test_leak.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    (pkg / "conftest.py").write_text(
        "import os\n\n"
        "import pytest\n\n"
        "# The 2026-07-29 leak shape: a session-scoped fixture mutates the process\n"
        "# env and has no restore. The package-scoped _e2e_process_env restore\n"
        "# fires when pytest leaves tests/e2e/, so a session-scoped mutation\n"
        "# persists past every package boundary — the class of leak the sentinels\n"
        "# exist to catch.\n"
        '@pytest.fixture(scope="session", autouse=True)\n'
        "def _leak_operator_secret():\n"
        '    os.environ["AVA_CLUSTER_SECRET"] = "mutated-sentinel-value"\n',
        encoding="utf-8",
    )
    return pkg


def test_trimmed_env_guard_sentinel_catches_a_leak(tmp_path: Path) -> None:
    leak_pkg = _write_leak_package(tmp_path)
    proc = subprocess.run(  # noqa: S603 — argv is our own venv python + fixed repo paths
        [
            sys.executable,
            "-m",
            "pytest",
            str(leak_pkg),
            "tests/test_home_isolation.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,  # the leak must fail the inner run — that IS the assertion
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, (
        "the env-guard sentinel did NOT fire on a simulated leak — the trimmed "
        "guard would certify an unprotected environment as protected.\n" + combined[-2000:]
    )
    assert "test_cluster_secret_is_the_test_secret_not_the_real_one" in combined, (
        "the simulated leak was not caught by the cluster-secret sentinel:\n" + combined[-2000:]
    )
