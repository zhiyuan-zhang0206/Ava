"""Durable root birth intent and observed native children.

An unfinished record blocks another root generation. It is evidence to reconcile,
never permission to signal a PID or assume an unacknowledged spawn did not occur.
This does not establish closure of independently registered execution domains.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from shared.native_process.ownership import OwnedProcess


def require_clear(run_dir: Path) -> None:
    """Refuse cold startup while a prior generation has unresolved custody."""
    directory = run_dir / "custody"
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError(f"root service custody requires reconciliation: {directory}")


def _flush_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ServiceCustody:
    """One unit's pending birth, retained until positively observed cleanup."""

    def __init__(self, run_dir: Path, unit: str) -> None:
        directory = run_dir / "custody"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / f"{unit}.json"
        self._body: dict[str, object] = {"version": 1, "unit": unit, "stage": "spawning"}
        self._expected = json.dumps(self._body)
        if os.name == "nt":
            from shared.root_control.windows.storage import publish

            publish(self.path, self._expected, exclusive=True)
            return
        _flush_directory(directory.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(self._expected)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            _flush_directory(directory)

    def retain(self, identities: set[OwnedProcess]) -> None:
        """Persist captured births before stopping any of those processes."""
        self._require_unchanged()
        self._body = self._body | {
            "stage": "running",
            "processes": [asdict(item) for item in sorted(identities, key=lambda p: p.pid)],
        }
        from shared.atomic_io import write_text_atomic

        self._expected = json.dumps(self._body)
        if os.name == "nt":
            from shared.root_control.windows.storage import publish

            publish(self.path, self._expected)
            return
        write_text_atomic(self.path, self._expected, mode=0o600, sync_parent=True)

    def clear(self) -> None:
        """Remove only this owner's record after completed native cleanup."""
        self._require_unchanged()
        if os.name == "nt":
            from shared.root_control.windows.storage import clear

            clear(self.path)
            return
        self.path.unlink()
        _flush_directory(self.path.parent)

    def _require_unchanged(self) -> None:
        if self.path.is_symlink() or self.path.read_text() != self._expected:
            raise RuntimeError(f"native custody changed; refusing mutation: {self.path}")
