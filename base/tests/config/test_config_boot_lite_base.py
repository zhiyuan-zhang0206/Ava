"""The skip mode defers config preparation and plants the placeholder database URL."""

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


def test_skip_mode_defers_prepare_and_plants_placeholders() -> None:
    proc = _spawn(
        "import os, base.config as c\n"
        "from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL\n"
        "prepared_before = c._boot_state()['prepared']\n"
        "value = c.settings.lm.llm_model\n"
        "print('DEFER', prepared_before, c._boot_state()['prepared'], value,\n"
        "      os.environ.get('AVA_DB_URL') == PLACEHOLDER_DB_URL)\n",
        env={"AVA_CONFIG_FETCH": "skip"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DEFER False True "), proc.stdout
    assert proc.stdout.rstrip().endswith("True"), proc.stdout
