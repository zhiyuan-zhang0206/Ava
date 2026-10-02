"""Settings-free identity of the home's authoritative startup configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from base.deploy.release.verified_file import regular_bytes


def configuration_digest(home: Path) -> str:
    """Include file-authoritative values omitted from child environment transport."""
    paths = [home / name for name in (".env", "plugins_config.json", "service-selection.json")]
    paths.extend(sorted((home / "configs").glob("*/config.json")))
    files = {str(path.relative_to(home)): _file_digest(path) for path in paths}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    # A dangling symlink is an invalid declaration, never an omitted/default
    # value. Disappearance or replacement after lstat also refuses this read.
    return hashlib.sha256(regular_bytes(path)).hexdigest()
