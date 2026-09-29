"""Declared file and directory inputs checked before every native unit birth.

These seals detect changed configuration; they do not turn mutable paths into
immutable artifacts. Publication must separately exclude concurrent writers.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_STABLE_STAT = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


class InputChangedError(OSError):
    """A declared input cannot authorize another birth of this generation."""


def _member_digest(path: Path) -> str:
    before = path.lstat()
    if stat.S_ISREG(before.st_mode):
        value: object = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
    elif stat.S_ISDIR(before.st_mode):
        value = ["directory", [(p.name, _member_digest(p)) for p in sorted(path.iterdir())]]
    else:
        raise InputChangedError(f"service input must be a regular file or directory: {path}")
    after = path.lstat()
    if any(getattr(before, key) != getattr(after, key) for key in _STABLE_STAT):
        raise InputChangedError(f"service input changed while reading: {path}")
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class InputSeal:
    """One canonical path and the expected identity of its complete contents."""

    path: Path
    digest: str

    @classmethod
    def capture(cls, path: Path) -> InputSeal:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise InputChangedError(f"service input path must be absolute and canonical: {path}")
        return cls(path, _member_digest(path))

    @classmethod
    def from_mapping(cls, raw: object) -> InputSeal:
        if not isinstance(raw, dict) or set(cast("dict[object, object]", raw)) != {
            "path",
            "digest",
        }:
            raise ValueError("service input must contain exactly path and digest")
        values = cast("dict[str, object]", raw)
        path, digest = values["path"], values["digest"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("service input path must be absolute")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("service input digest must be a lowercase SHA256")
        return cls(Path(path), digest)

    def as_mapping(self) -> dict[str, str]:
        return {"path": str(self.path), "digest": self.digest}

    def require_unchanged(self) -> None:
        if self.capture(self.path) != self:
            raise InputChangedError(
                f"service input changed since generation admission: {self.path}"
            )


def parse_inputs(raw: object) -> tuple[InputSeal, ...]:
    if not isinstance(raw, list):
        raise TypeError("service inputs must be a list")
    inputs = tuple(InputSeal.from_mapping(item) for item in cast("list[object]", raw))
    if len({item.path for item in inputs}) != len(inputs):
        raise ValueError("service inputs contain duplicate paths")
    return inputs
