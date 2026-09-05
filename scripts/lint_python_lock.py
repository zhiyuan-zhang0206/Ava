#!/usr/bin/env python3
"""Keep the shared Python lock on PyPI; regional mirrors belong to host config.

Run without project dependencies in CI and through pre-commit. A frozen sync
downloads the lock's artifact URLs even when the caller selects another index.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INDEX = "https://pypi.org/simple"
_ARTIFACT_PREFIX = "https://files.pythonhosted.org/packages/"


def violations(path: Path) -> list[str]:
    """Report registry packages whose index or artifact transport is host-specific."""
    with path.open("rb") as stream:
        lock = tomllib.load(stream)
    errors: list[str] = []
    for package in lock["package"]:
        source = package["source"]
        if "registry" not in source:
            continue
        name = package["name"]
        if source["registry"] != _INDEX:
            errors.append(f"{name}: registry must be {_INDEX}")
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if any(not item["url"].startswith(_ARTIFACT_PREFIX) for item in artifacts):
            errors.append(f"{name}: distribution URLs must use {_ARTIFACT_PREFIX}")
    return errors


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
