"""The workspace-owned document shared with the user, without a publish step."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ToUserFormat = Literal["html", "markdown"]
MAX_DOCUMENT_BYTES = 256 * 1024


class ToUserDocument(BaseModel):
    """An authoritative file snapshot; missing or empty files have no content."""

    model_config = ConfigDict(extra="forbid")
    format: ToUserFormat
    content: str | None
    version: str | None
    modified_at: datetime | None


class ToUserReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)


def document_filename(format: ToUserFormat) -> str:
    match format:
        case "html":
            return "to_user.html"
        case "markdown":
            return "to_user.md"
        case _:
            raise ValueError(f"Unknown To user format: {format!r}")


def read_document(workspace: Path, format: ToUserFormat) -> ToUserDocument:
    """Read only the fixed document, refusing symlinks, devices and oversized files.

    A directory descriptor pins the workspace while the agent may rename files.
    O_NONBLOCK prevents a FIFO from blocking the machine's ops worker.
    """
    filename = document_filename(format)
    empty = ToUserDocument(format=format, content=None, version=None, modified_at=None)
    try:
        directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return empty
    try:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return empty
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("To user must be a regular file")
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise ValueError("To user exceeds the 256 KiB document limit")
    finally:
        os.close(directory)
    content = raw.decode("utf-8")
    if not content.strip():
        return empty
    return ToUserDocument(
        format=format,
        content=content,
        version=hashlib.sha256(raw).hexdigest(),
        modified_at=datetime.fromtimestamp(info.st_mtime, UTC),
    )
