"""Terminal birth intent and exact native Job closure receipts.

Only the root creates an intent. Only its per-terminal owner advances that
record. A dead owner without an empty-Job receipt is unresolved custody; process
absence alone never permits replacement or a successful full stop.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.proc_tree import OwnedProcess


class NativeBirth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    pid: int = Field(gt=1)
    birth: float = Field(gt=0, allow_inf_nan=False)

    @classmethod
    def capture(cls, identity: OwnedProcess) -> NativeBirth:
        return cls(pid=identity.pid, birth=identity.birth)

    def identity(self) -> OwnedProcess:
        return OwnedProcess(self.pid, self.birth, None)


class TerminalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    domain: str = Field(pattern=r"^[a-f0-9]{32}$")
    generation: str | None
    state: Literal["pending", "running", "closed"]
    root: NativeBirth
    launcher: NativeBirth | None = None
    owner: NativeBirth | None = None
    target: NativeBirth | None = None
    started_at: float = Field(gt=0, allow_inf_nan=False)
    closed_at: float | None = None
    empty_job_observed: bool = False
    command: str
    cwd: str

    @model_validator(mode="after")
    def validate_stage(self) -> TerminalRecord:
        if self.state != "pending" and (self.owner is None or self.launcher is None):
            raise ValueError("terminal receipt omitted its native owner")
        if self.state == "running" and self.target is None:
            raise ValueError("running terminal omitted its captured target")
        if self.state == "closed":
            if not self.empty_job_observed or self.closed_at is None:
                raise ValueError("terminal closure requires an observed empty Job receipt")
            if not math.isfinite(self.closed_at) or self.closed_at < self.started_at:
                raise ValueError("terminal closure timestamp is invalid")
        elif self.closed_at is not None or self.empty_job_observed:
            raise ValueError("an active terminal cannot carry a closure receipt")
        return self


def record_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError("invalid terminal resource name")
    from shared.paths import run_dir

    return run_dir() / "windows-terminals" / f"{name}.json"


def endpoint(record: TerminalRecord) -> Path:
    return record_path(record.name).parent / f"{record.domain}.pipe"


def read(name: str) -> TerminalRecord | None:
    path = record_path(name)
    if path.is_symlink() or path.is_junction():
        raise RuntimeError("terminal custody must not be a link")
    try:
        record = TerminalRecord.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    if record.name != name:
        raise RuntimeError("terminal receipt names a different resource")
    return record


def publish(record: TerminalRecord, *, previous: TerminalRecord | None) -> None:
    """Replace only the unchanged predecessor; admission is externally locked."""
    from services.ava_root.windows.storage import publish as atomic_publish

    if read(record.name) != previous:
        raise RuntimeError("terminal custody changed; refusing mutation")
    path = record_path(record.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_publish(path, record.model_dump_json(), exclusive=previous is None)
