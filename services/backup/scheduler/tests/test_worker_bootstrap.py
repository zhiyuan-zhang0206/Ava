"""The operation worker's bootstrap runs the real backup worker module.

`worker_process._BOOTSTRAP` names the packages it requires inside the
controller's code root before it runs a worker. Naming a package that does not
exist makes EVERY scheduled dump and restore drill exit at start (the 2026-10
inventory's R1): the attempt fails without publishing an artifact, and the daily
backup is down until fixed. Each test starts the real bootstrap in the same
isolated interpreter the controller uses (`-I -B`), so a name that stops
resolving fails here, not at 03:00.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from base.native_process.child_env import inherited_process_env
from services.backup.scheduler.operation import worker_process

_ROOT = Path(__file__).resolve().parents[4]
_NO_REQUEST_USAGE = "usage: python -m <operation worker> REQUEST RESULT"


def _bootstrap(bootstrap: str, root: Path, module: str) -> subprocess.CompletedProcess[str]:
    """Run `module` under `bootstrap` with no request: it can only print its usage."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", "-c", bootstrap, str(root), module],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=inherited_process_env(),
        timeout=120,
    )


def _required_names() -> list[str]:
    match = re.search(r"for name in \((.*?)\):", worker_process._BOOTSTRAP)
    assert match is not None, "the bootstrap no longer names the packages it requires"
    return [item.strip().strip("\"'") for item in match.group(1).split(",")]


def test_the_real_bootstrap_reaches_the_backup_worker() -> None:
    """Every required name resolves inside the code root and the worker module
    imports completely: the only complaint left is the missing request."""
    # launch-ok: runs the production worker bootstrap source, the subject
    proc = _bootstrap(worker_process._BOOTSTRAP, _ROOT, "services.backup.scheduler.worker")

    assert "code root mismatch" not in proc.stderr, proc.stderr
    assert proc.returncode != 0
    assert _NO_REQUEST_USAGE in proc.stderr, proc.stderr


def test_every_required_name_is_a_package_of_this_checkout() -> None:
    """The names are packages or modules that exist, so none can be the stale one."""
    names = _required_names()

    assert names[-1] == "module", "the worker's own module is checked last"
    for name in names[:-1]:
        assert (_ROOT / Path(*name.split(".")) / "__init__.py").is_file() or (
            _ROOT / Path(*name.split(".")).with_suffix(".py")
        ).is_file(), f"the bootstrap requires {name}, which this checkout does not carry"


def test_a_required_name_that_resolves_nowhere_stops_the_worker_at_start() -> None:
    """The failure R1 describes: a stale name makes the bootstrap refuse the worker."""
    stale = worker_process._BOOTSTRAP.replace(
        '"services.backup.scheduler.operation"', '"services.retired_package"'
    )
    assert stale != worker_process._BOOTSTRAP

    # launch-ok: runs the production worker bootstrap source, the subject
    proc = _bootstrap(stale, _ROOT, "services.backup.scheduler.worker")

    assert proc.returncode != 0
    assert "code root mismatch: services.retired_package resolves to None" in proc.stderr


def test_the_bootstrap_still_refuses_code_outside_its_root(tmp_path: Path) -> None:
    # launch-ok: runs the production worker bootstrap source, the subject
    proc = _bootstrap(worker_process._BOOTSTRAP, tmp_path, "services.backup.scheduler.worker")

    assert proc.returncode != 0
    assert "code root mismatch" in proc.stderr
