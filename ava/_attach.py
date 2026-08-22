"""Register media for delivery across the child-to-parent attachment transport.

The child-local buffer is drained into the exec envelope, checkpointed by the
parent, and turned into one message when the current turn ends.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ava.files import _resolve
from shared.lm.attach import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_LABEL_CHARS,
    ATTACH_MEDIA_MIME,
)
from shared.log import logger

_ATTACHMENTS: list[dict[str, Any]] = []


def attach(path: str | Path, *, label: str | None = None) -> None:
    """Register a local media file for your next turn.

    Use this when your next response needs to inspect a file generated during
    this turn. The file is read when this turn ends, so keep it available until
    then. Your model receives supported media natively; unsupported media is
    described in a text note instead. Attach at most 8 files and 48 MiB per
    turn, with a 20 MiB limit per file. Raises an error outside an agent turn.
    """
    if not os.environ.get("AVA_EXEC_REQUEST_FILE"):
        raise RuntimeError(
            "ava.self.attach only works inside an agent turn (execute_code); "
            "outside a turn there is no runner to deliver the attachment"
        )
    resolved = _resolve(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"path {str(path)!r} does not name an existing file ({resolved})")
    if resolved.suffix.lower() not in ATTACH_MEDIA_MIME:
        supported = ", ".join(sorted(ATTACH_MEDIA_MIME))
        raise ValueError(
            f"unsupported attachment suffix {resolved.suffix!r}; supported suffixes: {supported}; "
            "text files belong in exec output or ava.understand"
        )
    if resolved.stat().st_size > ATTACH_MAX_FILE_BYTES:
        raise ValueError(f"attachment exceeds the 20 MiB per-file limit: {resolved}")
    if not isinstance(label, str | None):
        raise TypeError("attachment label must be a str or None")
    if label is not None and len(label) > ATTACH_MAX_LABEL_CHARS:
        raise ValueError(f"attachment label exceeds {ATTACH_MAX_LABEL_CHARS} characters")
    _ATTACHMENTS.append({"path": str(resolved), "label": label})


def take_attachments() -> list[dict[str, Any]]:
    """Drain registered attachments for the exec child result envelope."""
    entries = list(_ATTACHMENTS)
    _ATTACHMENTS.clear()
    accepted: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("dropping tampered attachment registration with non-dict entry")
            continue
        path = entry.get("path")
        label = entry.get("label")
        if not isinstance(path, str) or not isinstance(label, str | None):
            logger.warning("dropping tampered attachment registration with invalid shape")
            continue
        accepted.append({"path": path, "label": label})
    return accepted
