"""Validate exec attachment registrations before parking them in checkpoint state."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, cast

from agent.state import AttachEntry, AttachState
from shared.lm.attach import ATTACH_MAX_FILE_BYTES, ATTACH_MEDIA_MIME
from shared.log import logger


def merge_attachments(pending: AttachState, entries: list[dict[str, Any]] | None) -> AttachState:
    """Merge valid child registrations into pending attachments without reading bytes."""
    merged: list[AttachEntry] = []
    positions: dict[str, int] = {}
    for entry in pending.pending:
        path = str(Path(entry.path).resolve())
        prior = positions.get(path)
        replacement = AttachEntry(path=path, label=entry.label)
        if prior is None:
            positions[path] = len(merged)
            merged.append(replacement)
        else:
            merged[prior] = replacement

    if entries is None:
        return pending.model_copy(update={"pending": merged})
    if not isinstance(entries, list):
        logger.warning("dropping tampered exec attachment payload with non-list entries")
        return pending.model_copy(update={"pending": merged})

    for entry in entries:
        attachment = _validated_attachment(entry)
        if attachment is None:
            continue
        prior = positions.get(attachment.path)
        if prior is None:
            positions[attachment.path] = len(merged)
            merged.append(attachment)
        else:
            merged[prior] = attachment
    return pending.model_copy(update={"pending": merged})


def _validated_attachment(entry: object) -> AttachEntry | None:
    if not isinstance(entry, dict):
        logger.warning("dropping exec attachment with non-dict entry")
        return None
    raw_entry = cast("dict[str, Any]", entry)
    path = raw_entry.get("path")
    label = raw_entry.get("label")
    if not isinstance(path, str) or not isinstance(label, str | None):
        logger.warning("dropping exec attachment with invalid path or label")
        return None
    try:
        resolved = Path(path).resolve()
        file_stat = resolved.stat()
    except (OSError, ValueError):
        logger.warning("dropping exec attachment that cannot be statted: {path}", path=path)
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        logger.warning("dropping exec attachment that is not a regular file: {path}", path=resolved)
        return None
    if resolved.suffix.lower() not in ATTACH_MEDIA_MIME:
        logger.warning("dropping exec attachment with unsupported suffix: {path}", path=resolved)
        return None
    if file_stat.st_size > ATTACH_MAX_FILE_BYTES:
        logger.warning("dropping exec attachment over the per-file limit: {path}", path=resolved)
        return None
    return AttachEntry(path=str(resolved), label=label)
