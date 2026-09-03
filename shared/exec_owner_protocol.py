"""Private per-exec owner messages; none are authority without DB allocation CAS."""

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.incarnation_resources import ExecAllocation

MAX_OWNER_MESSAGE = 16 * 1024


class OwnerContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    agent_id: int = Field(gt=0)
    generation: UUID
    runtime_owner: UUID
    request_path: Path
    result_path: Path
    allocation: ExecAllocation

    @model_validator(mode="after")
    def require_canonical_request(self) -> "OwnerContext":
        for path in (self.request_path, self.result_path):
            if not path.is_absolute() or path.resolve() != path:
                raise ValueError("exec owner envelope path must be canonical and absolute")
        if self.request_path.parent != self.result_path.parent:
            raise ValueError("exec owner envelopes must share their private request directory")
        if self.request_path.name != f"req-{self.allocation.request.hex}.json":
            raise ValueError("exec owner request path differs from allocation identity")
        if self.allocation.owner_process is not None:
            raise ValueError("owner context cannot predeclare native domain identities")
        return self


class OwnerControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    request: UUID
    domain: UUID
    action: Literal["permit", "cancel"]


class OwnerReady(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    allocation: ExecAllocation


class OwnerClosed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    allocation: ExecAllocation
    root_exit_code: int
    reason: Literal["completed", "host_eof", "cancel", "timeout"]
    observed_at: datetime


def read_owner_context(path: Path) -> OwnerContext:
    return OwnerContext.model_validate_json(read_owner_bytes(path))


def read_owner_bytes(path: Path, limit: int = MAX_OWNER_MESSAGE) -> bytes:
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("owner context must be a canonical private file")
    before = path.lstat()
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ValueError("owner evidence changed or is not a regular file")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if (opened.st_size, opened.st_mtime_ns, opened.st_ino, opened.st_dev) != (
        after.st_size,
        after.st_mtime_ns,
        current.st_ino,
        current.st_dev,
    ):
        raise ValueError("owner evidence changed while reading")
    if len(content) > limit:
        raise ValueError("owner context exceeds its bounded envelope")
    return content


def publish_owner_message(path: Path, message: OwnerReady | OwnerClosed | OwnerContext) -> None:
    """Publish complete bytes once; a crash leaves no valid partial terminal file."""
    part = path.with_suffix(path.suffix + ".part")
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        content = message.model_dump_json().encode()
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike replace, link refuses a preexisting receipt (including a replay).
        os.link(part, path)
    finally:
        part.unlink(missing_ok=True)
