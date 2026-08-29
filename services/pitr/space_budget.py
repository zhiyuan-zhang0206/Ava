"""Fail-closed local disk budget for base-backup candidates."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CandidateSpaceBudget:
    compressed_staging_estimate: int
    spool_and_pg_wal_reserve: int
    logical_backup_peak_reserve: int
    emergency_floor: int

    @property
    def required_bytes(self) -> int:
        return (
            self.compressed_staging_estimate
            + self.spool_and_pg_wal_reserve
            + self.logical_backup_peak_reserve
            + self.emergency_floor
        )


class InsufficientCandidateSpaceError(RuntimeError):
    pass


def require_candidate_space(path: Path, budget: CandidateSpaceBudget) -> int:
    free = shutil.disk_usage(path).free
    if free < budget.required_bytes:
        raise InsufficientCandidateSpaceError(
            f"base candidate needs {budget.required_bytes} free bytes; {free} available"
        )
    return free
