#!/usr/bin/env python3
"""Keep the committed npm lock on the public registry; mirrors are transport.

Run without project dependencies in CI and through pre-commit. npm rewrites the
download host from registry.npmjs.org to the configured registry at install
time, so a canonical lock still installs through a mirror -- but an entry
materialized under a mirror records the mirror URL, which must never reach the
committed lock.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY = "https://registry.npmjs.org/"


def violations(path: Path) -> list[str]:
    """Report lock entries whose ``resolved`` URL left the public registry."""
    with path.open("rb") as stream:
        lock = json.load(stream)
    errors: list[str] = []
    for entry_path, entry in lock["packages"].items():
        resolved = entry.get("resolved")
        if not isinstance(resolved, str):
            continue
        if not resolved.startswith(_REGISTRY):
            errors.append(f"{entry_path or '(root)'}: resolved must start with {_REGISTRY}")
    return errors


def main() -> int:
    """Check only the repository lock, leaving explicit machine mirror profiles alone."""
    errors = violations(_REPO_ROOT / "ui" / "web" / "package-lock.json")
    for error in errors[:10]:
        print(f"ui/web/package-lock.json: {error}", file=sys.stderr)
    if errors:
        print(
            f"{len(errors)} noncanonical lock entries. Keep mirror settings to host "
            "transport and add or regenerate dependencies against registry.npmjs.org.",
            file=sys.stderr,
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
