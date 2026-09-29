"""PITR execution receipts; the activation record owns all business evidence.

These settings-free values bind one action to its startup inputs and native
stop custody. They cannot discover or adopt a process during recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cli.release_transition.request import Digest, Record
from shared.native_process.ownership import OwnedProcess
from shared.process_evidence import ExpectedProcess


class DataOwner(Record):
    process: ExpectedProcess
    tree: tuple[ExpectedProcess, ...]
    directory: str
    port: int = Field(gt=0, le=65535)
    config_digest: Digest | None = None

    @property
    def identity(self) -> OwnedProcess:
        p = self.process
        return OwnedProcess(p.pid, p.create_time, p.starttime)

    @property
    def identities(self) -> set[OwnedProcess]:
        return {OwnedProcess(p.pid, p.create_time, p.starttime) for p in self.tree}

    @model_validator(mode="after")
    def captured_tree(self) -> Self:
        if self.process not in self.tree or not Path(self.directory).is_absolute():
            raise ValueError("data custody requires the exact leader tree and absolute resource")
        return self


class DataStop(Record):
    postgres: DataOwner
    redis: DataOwner
    pgbouncer: DataOwner | None

    def owners(self) -> dict[str, DataOwner]:
        result = {"postgres": self.postgres, "redis": self.redis}
        if self.pgbouncer is not None:
            result = {"pgbouncer": self.pgbouncer, **result}
        return result


class PitrSeal(Record):
    configuration_digest: Digest
    auto_conf_digest: Digest
    archive_settings_digest: Digest
    postgres: ExpectedProcess
    postgres_state: dict[str, str]
    activation_digest: Digest


class PitrProgress(Record):
    action: Literal["activate", "rollback"]
    generation: int = Field(ge=1)
    maintenance_at: AwareDatetime
    record_digest: Digest | None
    record_intent: tuple[Digest | None, Digest] | None = None
    seal: PitrSeal | None = None
    data_stop: DataStop | None = None
    decisions: tuple[dict[str, str | int], ...] = ()
