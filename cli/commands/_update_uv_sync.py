"""Native update-chain seam for a permission-tolerant ``uv sync``."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from cli.commands._repo import _repo_root
from shared.editable_install import editable_pth_write_window


def run_uv_sync(
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> subprocess.CompletedProcess[bytes]:
    """Run ``uv sync`` while preserving the editable pointer's exact mode."""
    with editable_pth_write_window(repo):
        return runner(
            ["uv", "sync"],
            cwd=repo,
            capture_output=False,
            check=False,
        )


def _main() -> int:
    repo = _repo_root()
    return run_uv_sync(repo, runner=subprocess.run).returncode


if __name__ == "__main__":
    raise SystemExit(_main())
