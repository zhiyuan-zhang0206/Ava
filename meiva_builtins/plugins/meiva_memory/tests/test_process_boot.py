"""Eval-isolation boot enforcement runs after plugins have registered the SDK."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CHILD = Path(__file__).with_name("child_eval_isolation.py")


def _run(scenario: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
            [sys.executable, str(_CHILD), scenario],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "MEIVA_HOME": home,
                "MEIVA_CONFIG_FETCH": "skip",
                **_settings_env(),
            },
            check=False,
        )
    return proc.returncode, proc.stdout, proc.stderr


def _settings_env() -> dict[str, str]:
    return {
        key: os.environ[key] for key in ("MEIVA_DB_URL", "MEIVA_REDIS_URL") if key in os.environ
    }


def test_eval_isolation_disables_network_and_result_sdk_surfaces() -> None:
    code, out, err = _run("isolated")
    assert code == 0, err
    assert out.strip() == "ok"


def test_eval_network_allowlist_preserves_explicitly_allowed_web() -> None:
    code, out, err = _run("network_allowlist")
    assert code == 0, err
    assert out.strip() == "ok"


def test_eval_isolation_off_leaves_sdk_and_memory_unchanged() -> None:
    code, out, err = _run("off")
    assert code == 0, err
    assert out.strip() == "ok"
