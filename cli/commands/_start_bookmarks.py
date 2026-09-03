"""Best-effort source bookmarks written after a successful start."""

from __future__ import annotations

import subprocess
from pathlib import Path


def record_running_sha(repo: Path) -> None:
    """Record the current HEAD for update change detection without failing start."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            from shared import running_sha

            running_sha.set(result.stdout.strip())
    except Exception as exc:
        print(f"  · running-sha bookmark skipped ({exc})")
