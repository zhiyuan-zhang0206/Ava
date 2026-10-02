"""`pool_ops.py` resolves the home the way every other Ava process does.

`ava_builtins/plugins/ava_memory/pool_ops.py` is imported by the consolidation
scripts under `skills/scripts/` (`consolidate.py`, `steward.py`, `arbiter_merge.py`,
`gen_indexes.py`, `rebuild_memory_index.py`) as
`ava_builtins.plugins.ava_memory.pool_ops`, so a run needs the checkout's venv
(`ava_builtins` importable). Its home is `base.host.env.dotenv_boot.resolve_ava_home`
(`$AVA_HOME`, else `~/.ava`), which builds no Settings. `pool_dir()` /
`refresh_index()` are write paths (git commit + push to the pool, `ava memory
refresh`), so the default-home cases run under a fake HOME: nothing here can touch
the operator's real `~/.ava`.

Runs `pool_ops.py` in a subprocess with a from-scratch environment (like
`test_ava_memory_steward_guard.py`'s `env = os.environ.copy()` pattern): the
scripts run as their own processes, so a real process environment is the seam.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# argv: <function to call>. Prints the result.
_DRIVER = r"""
import sys

from ava_builtins.plugins.ava_memory import pool_ops

fn = sys.argv[1]
if fn == "pool_dir":
    print(str(pool_ops.pool_dir()))
elif fn == "machine_name":
    print(pool_ops.machine_name())
else:
    raise ValueError(f"unknown fn {fn!r}")
"""


def _run(
    fn: str, env_overrides: dict[str, str], home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    # Start from a clean slate: no inherited AVA_* leaks from this test's own
    # process, so each case's environment is exactly what it declares.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    if home is not None:
        env["HOME"] = str(home)
    env |= env_overrides
    return subprocess.run(  # noqa: S603 — fixed argv, repository-owned driver script
        [sys.executable, "-c", _DRIVER, fn],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_pool_dir_is_under_the_default_home_when_ava_home_is_unset(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    res = _run("pool_dir", {}, home=fake_home)

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(fake_home / ".ava" / "memory")


def test_pool_dir_is_relative_to_the_explicit_home(tmp_path: Path) -> None:
    home = tmp_path / "dev-cluster-home"

    res = _run("pool_dir", {"AVA_HOME": str(home)})

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(home / "memory")


def test_machine_name_short_circuits_on_its_own_env_var() -> None:
    """AVA_MACHINE_NAME alone is enough — no home file is read for this path."""
    res = _run("machine_name", {"AVA_MACHINE_NAME": "testbox"})

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "testbox"


def test_machine_name_reads_the_default_homes_env_file_when_ava_home_is_unset(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".ava").mkdir(parents=True)
    (fake_home / ".ava" / ".env").write_text("OTHER=kept\nAVA_MACHINE_NAME=planted-runner\n")

    res = _run("machine_name", {}, home=fake_home)

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "planted-runner"


def test_machine_name_refuses_when_nothing_names_the_machine(tmp_path: Path) -> None:
    """A `machine_name` file older homes kept is not read, and there is no made-up
    fallback: the steward would push under `machine-unknown`."""
    fake_home = tmp_path / "home"
    (fake_home / ".ava").mkdir(parents=True)
    (fake_home / ".ava" / "machine_name").write_text("planted-runner\n")

    res = _run("machine_name", {}, home=fake_home)

    assert res.returncode != 0
    assert "AVA_MACHINE_NAME" in res.stderr
    assert res.stdout.strip() == ""


# ── import side effects (path_imports refactor, pool_ops now a plugin submodule) ──

_IMPORT_SIDE_EFFECT_DRIVER = r"""
import sys

before = set(sys.modules)
from ava_builtins.plugins.ava_memory import pool_ops  # noqa: F401
after = set(sys.modules)

# Nothing beyond pool_ops itself and its own (stdlib) imports should load as a
# side effect -- in particular, no sibling submodule that does plugin/hook
# registration (plugin.py, sdk.py, services.py, setup.py, ...).
new_ava_builtins_modules = sorted(
    m for m in (after - before) if m.startswith("ava_builtins.plugins.ava_memory")
)
print(",".join(new_ava_builtins_modules))
"""


def test_importing_pool_ops_outside_a_plugin_context_registers_nothing() -> None:
    """Importing `ava_builtins.plugins.ava_memory.pool_ops` as a plain module
    (the way the consolidation scripts do, outside any `PluginContext`) must not
    pull in the plugin's registration surface (`plugin.py`, `sdk.py`,
    `services.py`, ...) as a side effect. `ava_builtins/__init__.py`,
    `ava_builtins/plugins/__init__.py`, and `ava_builtins/plugins/ava_memory/
    __init__.py` are all side-effect-free (no imports, no registration calls),
    which this fresh-subprocess import locks in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    res = subprocess.run(  # noqa: S603 — fixed argv, repository-owned driver script
        [sys.executable, "-c", _IMPORT_SIDE_EFFECT_DRIVER],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert res.returncode == 0, res.stderr
    loaded = [m for m in res.stdout.strip().split(",") if m]
    # The import chain always registers each parent package too (Python's own
    # import machinery, not a side effect of this module); no OTHER submodule
    # (plugin.py, sdk.py, services.py, setup.py, ...) may appear.
    assert loaded == [
        "ava_builtins.plugins.ava_memory",
        "ava_builtins.plugins.ava_memory.pool_ops",
    ]
