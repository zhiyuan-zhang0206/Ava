"""Windows import-surface regression for the services/pitr module tree.

CI has no Windows runner — the root cause of the 2026-08-29 outage where a
module-level ``import fcntl`` in services/pitr/archive_shim crashed every CLI
command on Windows units (fixed by lazy-import in 311b40c62; the shim is
imported by the CLI through services/pitr/retention_planner). These tests
simulate Windows's missing fcntl in a fresh interpreter and assert that every
module in the tree still imports, without replacing modules used by other tests.

Trade-off vs a real Windows runner: only the fcntl absence is simulated, not
msvcrt or other platform quirks — but the ImportError mechanism is exactly
what broke Windows boot, and the module set is enumerated from the package
directory so a newly added module is covered automatically.
"""

from __future__ import annotations

import builtins
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import services.pitr

PITR_MODULES = sorted(
    path.stem
    for path in Path(services.pitr.__file__).resolve().parent.glob("*.py")
    if path.stem != "__init__"
)


def _no_fcntl_import(
    name: str,
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: Sequence[str] | None = None,
    level: int = 0,
) -> Any:
    if name == "fcntl":
        raise ImportError("No module named 'fcntl'")
    return _real_import(name, globals, locals, fromlist, level)


_real_import = builtins.__import__


def test_module_enumeration_is_nonempty() -> None:
    """Guard the parametrization: an empty module list would silently skip
    every import test instead of failing loud."""
    assert len(PITR_MODULES) >= 30


@pytest.mark.parametrize("module_name", PITR_MODULES)
def test_pitr_module_imports_without_fcntl(module_name: str) -> None:
    """The module's top-level code must not depend on fcntl.

    A fresh interpreter executes the import even when collection already
    loaded it. Replacing sys.modules in pytest leaves existing function
    references attached to old globals and breaks multiprocessing pickling.
    """
    script = """
import builtins
import sys
from tests.services.test_pitr_windows_import_surface import _no_fcntl_import

assert sys.argv[1] not in sys.modules
builtins.__import__ = _no_fcntl_import
module = builtins.__import__(sys.argv[1], fromlist=("*",))
assert module.__name__ == sys.argv[1]
"""
    completed = subprocess.run(  # noqa: S603 — fixed interpreter and test script.
        [sys.executable, "-c", script, f"services.pitr.{module_name}"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_no_fcntl_fake_rejects_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the suite's premise is that the fake raises for
    fcntl — if the interception ever stops working, every import test above
    would pass vacuously. Pin the premise itself."""
    monkeypatch.setattr(builtins, "__import__", _no_fcntl_import)
    with pytest.raises(ImportError):
        builtins.__import__("fcntl", globals(), locals())
