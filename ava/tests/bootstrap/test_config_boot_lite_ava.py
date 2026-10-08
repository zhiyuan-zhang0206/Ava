"""`import ava` keeps the config boot lite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.pop("VIRTUAL_ENV", None)
    env.update(overrides)
    return env


def _spawn(
    code: str, *, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", "-c", code],
        check=False,
        cwd=_REPO_ROOT,
        env=_clean_env(**(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_import_ava_stays_lite(tmp_path: Path) -> None:
    proc = _spawn(
        "import sys, ava\n"
        "import base.config as c\n"
        "st = c._boot_state()\n"
        "print('AVA', st['mode'], st['upgrades'], 'pydantic_settings' in sys.modules, "
        "'base.config.base' in sys.modules)\n",
        env={"AVA_HOME": str(tmp_path), "AVA_CONFIG_FETCH": "skip"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("AVA lite 0 False False"), proc.stdout
