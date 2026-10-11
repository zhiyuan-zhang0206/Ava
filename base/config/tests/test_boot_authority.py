"""Database authority follows the actual eager, lite and deferred config boot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_CHILD = Path(__file__).with_name("_boot_authority_child.py")


@pytest.mark.parametrize("mode", ["lite", "eager", "skip"])
def test_boot_authority_result_reaches_the_database_handle(
    mode: str, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    """The actual config boot keeps a refused delivery through full upgrade."""
    home = tmp_path / "boot-home"
    home.mkdir()
    seed_write_generation(home)
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://ava@127.0.0.1:6433/ava\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n",
        encoding="utf-8",
    )
    intent = home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(home), "checkout": str(tmp_path / "foreign")}))
    intent.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.pop("VIRTUAL_ENV", None)
    env["AVA_HOME"] = str(home)
    if mode == "eager":
        env["AVA_CONFIG_BOOT"] = "eager"
    if mode == "skip":
        env["AVA_CONFIG_FETCH"] = "skip"
    proc = subprocess.run(  # noqa: S603 — this test owns its isolated home and argv
        [sys.executable, "-B", str(_CHILD)],
        check=False,
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED refusing to dial" in proc.stdout
    assert "source checkout" in proc.stdout
