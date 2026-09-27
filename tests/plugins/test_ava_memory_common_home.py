"""`_common.py::ava_home` never guesses `~/.ava` (2026-09-27 fix).

`ava_builtins/plugins/ava_memory/skills/scripts/_common.py` is deliberately
self-contained (stdlib + subprocess only, no `shared` import — see its module
docstring) because these scripts are copied with the skill and may run on a
machine that carries only the pool checkout, not the Ava source tree. That
rules out `shared.dotenv_boot.resolve_ava_home`'s checkout-anchored
resolution, so the fix here is the opposite, simpler stance: require an
explicit `AVA_HOME` and fail fast instead of defaulting to `Path.home() /
".ava"` — the same "unanchored checkout reaches production" bug class as
`shared/dotenv_boot.py`, but for a script with no checkout identity to anchor
to at all. `pool_dir()` / `refresh_index()` are write paths (git commit +
push to the pool, `ava memory refresh`), so a wrong guess would not just
misread a stray file, it would mutate whatever machine happens to run this.

Runs `_common.py` in a subprocess with a from-scratch environment (like
`test_ava_memory_steward_guard.py`'s `env = os.environ.copy()` pattern)
rather than `monkeypatch.setenv`/`delenv` on `AVA_HOME` — `AVA_HOME` is also
a `shared.config.Settings` field alias, and `lint_no_os_environ.py` (Rule 2)
correctly flags `monkeypatch.setenv` on it as a Settings-singleton no-op
footgun everywhere else in the suite; `_common.py` reads raw `os.environ` by
design, so a real subprocess environment is the actual seam here, not a
monkeypatch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = (
    Path(__file__).parents[2] / "ava_builtins" / "plugins" / "ava_memory" / "skills" / "scripts"
)

# argv: <scripts dir> <function to call>. Prints the result on success;
# on SystemExit (the fail-fast path), prints "SYSTEMEXIT: <message>" and
# exits 1 so the test can tell a refusal apart from a crash.
_DRIVER = r"""
import sys

sys.path.insert(0, sys.argv[1])
import _common

try:
    fn = sys.argv[2]
    if fn == "ava_home":
        print(str(_common.ava_home()))
    elif fn == "pool_dir":
        print(str(_common.pool_dir()))
    elif fn == "machine_name":
        print(_common.machine_name())
    else:
        raise ValueError(f"unknown fn {fn!r}")
except SystemExit as exc:
    print(f"SYSTEMEXIT: {exc}")
    sys.exit(1)
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
        [sys.executable, "-c", _DRIVER, str(_SCRIPTS_DIR), fn],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_ava_home_refuses_when_unset() -> None:
    res = _run("ava_home", {})

    assert res.returncode == 1
    assert "SYSTEMEXIT: AVA_HOME is not set" in res.stdout


def test_ava_home_never_falls_back_to_the_default_home(tmp_path: Path) -> None:
    """A planted look-alike `~/.ava` under a fake HOME must never be picked up
    when AVA_HOME itself is unset — there is no fallback path to it at all."""
    fake_home = tmp_path / "home"
    planted = fake_home / ".ava"
    planted.mkdir(parents=True)
    (planted / "machine_name").write_text("planted-prod-runner\n")

    res = _run("ava_home", {}, home=fake_home)

    assert res.returncode == 1
    assert "SYSTEMEXIT: AVA_HOME is not set" in res.stdout
    assert str(planted) not in res.stdout


def test_ava_home_uses_the_explicit_env_var(tmp_path: Path) -> None:
    home = tmp_path / "dev-cluster-home"

    res = _run("ava_home", {"AVA_HOME": str(home)})

    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == str(home)


def test_pool_dir_requires_ava_home() -> None:
    res = _run("pool_dir", {})

    assert res.returncode == 1
    assert "SYSTEMEXIT: AVA_HOME is not set" in res.stdout


def test_pool_dir_is_relative_to_the_explicit_home(tmp_path: Path) -> None:
    home = tmp_path / "dev-cluster-home"

    res = _run("pool_dir", {"AVA_HOME": str(home)})

    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == str(home / "memory")


def test_machine_name_short_circuits_on_its_own_env_var() -> None:
    """AVA_MACHINE_NAME alone is enough — no AVA_HOME needed for this path."""
    res = _run("machine_name", {"AVA_MACHINE_NAME": "testbox"})

    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "testbox"


def test_machine_name_refuses_rather_than_reading_the_default_home() -> None:
    res = _run("machine_name", {})

    assert res.returncode == 1
    assert "SYSTEMEXIT: AVA_HOME is not set" in res.stdout
