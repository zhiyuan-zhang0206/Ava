"""Settings-free identity of the home's authoritative startup configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shared.runtime_release import ReleaseRejectedError
from shared.verified_file import regular_bytes


def configuration_digest(home: Path) -> str:
    """Include file-authoritative values omitted from child environment transport."""
    return files_digest(configuration_files(home))


def configuration_files(home: Path) -> dict[str, str | None]:
    """Capture the authoritative set so an owned edit can bind its exact postimage."""
    paths = [home / name for name in (".env", "plugins_config.json", "service-selection.json")]
    paths.extend(sorted((home / "configs").glob("*/config.json")))
    return {str(path.relative_to(home)): _file_digest(path) for path in paths}


def files_digest(files: dict[str, str | None]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    # A dangling symlink is an invalid declaration, never an omitted/default
    # value. Disappearance or replacement after lstat also refuses this read.
    return hashlib.sha256(regular_bytes(path)).hexdigest()


def require_configuration(home: Path, expected: str) -> None:
    """Refuse configuration drift without loading Settings or writing any state."""
    if configuration_digest(home) != expected:
        raise ReleaseRejectedError("release configuration changed after preparation")
