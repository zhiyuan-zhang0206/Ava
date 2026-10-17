"""`import meiva` keeps the config boot lite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHILD = Path(__file__).with_name("child_sdk_probes.py")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.pop("VIRTUAL_ENV", None)
    env.update(overrides)
    return env


def _spawn(
    scenario: str, *, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", str(_CHILD), scenario],
        check=False,
        cwd=_REPO_ROOT,
        env=_clean_env(**(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_import_meiva_stays_lite(tmp_path: Path) -> None:
    proc = _spawn(
        "config_lite",
        env={"MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("MEIVA lite 0 False False"), proc.stdout
