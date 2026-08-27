"""Negative probe for the contextvars allowlist lock (ruff TID251).

Every real `contextvars` import in the repo lives in an allowlisted file
(`pyproject.toml` — `flake8-tidy-imports.banned-api` + `per-file-ignores`),
so the CI `ruff check` step alone cannot prove the ban is live: a broken or
removed ban config would still pass, because no scanned file triggers it.
This probe runs ruff against a scratch file that imports `contextvars` and
asserts the ban fires. The scratch file lives in pytest's tmp_path (outside
the repo), so no allowlist `per-file-ignores` pattern can match it; the repo
`pyproject.toml` is passed explicitly via `--config`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUFF = Path(sys.executable).parent / ("ruff.exe" if sys.platform == "win32" else "ruff")


def _ruff_check(path: Path) -> subprocess.CompletedProcess[str]:
    # S603: the command is internally derived (venv ruff binary + repo
    # pyproject.toml + pytest tmp path) — never external input.
    return subprocess.run(  # noqa: S603
        [_RUFF, "check", "--config", str(_REPO_ROOT / "pyproject.toml"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_contextvars_import_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    probe = tmp_path / "not_an_owner.py"
    probe.write_text("import contextvars\n", encoding="utf-8")

    result = _ruff_check(probe)

    assert result.returncode == 1
    assert "TID251" in result.stdout
    assert "contextvars is allowlisted only" in result.stdout
