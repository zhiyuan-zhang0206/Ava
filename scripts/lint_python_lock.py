#!/usr/bin/env python3
"""Keep the shared Python lock on PyPI; regional mirrors belong to host config.

Run without project dependencies in CI and through pre-commit. A frozen sync
downloads the lock's artifact URLs even when the caller selects another index.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from shared.python_lock import violations  # noqa: E402 — standalone dependency-free entry point


def main() -> int:
    """Check only the repository lock, leaving explicit machine mirror profiles alone."""
    errors = violations(_REPO_ROOT / "uv.lock")
    for error in errors[:10]:
        print(f"uv.lock: {error}", file=sys.stderr)
    if errors:
        print(
            f"{len(errors)} noncanonical lock entries. Keep --mirror cn local; "
            "regenerate the committed lock against PyPI without changing package pins.",
            file=sys.stderr,
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
