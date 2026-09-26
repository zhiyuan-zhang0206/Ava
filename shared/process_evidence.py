"""Strict runtime evidence and read-only exact process observations.

Release builders and health endpoints share these value types without importing
rollout leases, database fences or retired updater orchestration. An observation
never grants startup authority; unreadable identity remains explicitly unknown.
"""

from __future__ import annotations

import sys
from typing import Annotated, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field

from shared.native_process import pid_starttime_ticks
from shared.native_process.ownership import create_time_matches, stable_create_time

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExpectedProcess(EvidenceModel):
    pid: int = Field(gt=0)
    create_time: float = Field(gt=0, allow_inf_nan=False)
    starttime: int | None = Field(default=None, gt=0)


ProcessVerdict = Literal["alive", "exited", "identity_mismatch", "unknown"]


def observe_process(expected: ExpectedProcess) -> ProcessVerdict:
    """A reused PID is not the expected process and is not silently accepted.

    A /proc entry that vanished between psutil's validation and the identity
    read is the exit itself (the process was reaped in between) — `exited`,
    not a lost observation; a pid that still exists but could not be read
    stays `unknown`.
    """
    try:
        process = psutil.Process(expected.pid)
        if sys.platform == "linux" and expected.starttime is None:
            return "unknown"
        if expected.starttime is not None:
            actual = pid_starttime_ticks(expected.pid)
            if actual is None:
                if psutil.pid_exists(expected.pid):
                    return "unknown"
                return "exited"
            if actual != expected.starttime:
                return "identity_mismatch"
        elif not create_time_matches(stable_create_time(process), expected.create_time):
            # Non-Linux native timestamps compare exactly.
            return "identity_mismatch"
        return "exited" if process.status() == psutil.STATUS_ZOMBIE else "alive"
    except psutil.NoSuchProcess:
        return "exited"
    except (psutil.AccessDenied, OSError):
        return "unknown"
