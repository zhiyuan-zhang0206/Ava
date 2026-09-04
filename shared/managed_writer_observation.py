"""Read-only exact process/session observations; never a fleet closure assertion.

The prepared inventory producer owns completeness. These facts cannot authorize
admission: unsupported launchers, changed identities and unreadable state refuse.
No Settings, ordinary ops handlers, process signals or session mutation are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psutil
from pydantic import AwareDatetime, Field, ValidationError

from shared.managed_writer_barrier import Digest, EvidenceModel, ManagedUnit
from shared.session_record import pid_starttime_ticks


class ExpectedProcess(EvidenceModel):
    pid: int = Field(gt=0)
    create_time: float = Field(gt=0, allow_inf_nan=False)
    starttime: int | None = Field(default=None, gt=0)


class ExpectedSession(EvidenceModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    process: ExpectedProcess
    generation: str | None = Field(default=None, min_length=1, max_length=128)


class ExpectedLauncher(EvidenceModel):
    kind: Literal["launchd", "crontab", "schtasks"]
    name: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    definition_digest: Digest


class ExpectedUnitWriters(EvidenceModel):
    """Prepared unit inventory, not observed closure or an authorization token."""

    version: Literal[1] = 1
    machine: str = Field(min_length=1, max_length=128)
    home: str
    artifact_digest: Digest
    manifest_digest: Digest
    processes: tuple[ExpectedProcess, ...]
    sessions: tuple[ExpectedSession, ...]
    launchers: tuple[ExpectedLauncher, ...]

    def unit(self) -> ManagedUnit:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return ManagedUnit(
            machine=self.machine,
            home=self.home,
            inventory_digest=hashlib.sha256(body.encode()).hexdigest(),
        )


ProcessVerdict = Literal["alive", "exited", "identity_mismatch", "unknown"]


def observe_process(expected: ExpectedProcess) -> ProcessVerdict:
    """A reused PID is not the expected process and is not silently accepted."""
    try:
        process = psutil.Process(expected.pid)
        if expected.starttime is not None:
            actual = pid_starttime_ticks(expected.pid)
            if actual is None:
                return "unknown"
            if actual != expected.starttime:
                return "identity_mismatch"
        elif process.create_time() != expected.create_time:
            return "identity_mismatch"
        return "exited" if process.status() == psutil.STATUS_ZOMBIE else "alive"
    except psutil.NoSuchProcess:
        return "exited"
    except (psutil.AccessDenied, OSError):
        return "unknown"


SessionVerdict = Literal["absent", "record_present", "identity_mismatch", "unknown"]


def observe_session(home: Path, expected: ExpectedSession) -> SessionVerdict:
    """Do not use SessionRecord.read: it collapses malformed/unreadable to absent."""
    directory = home / "run" / "sessions"
    path = directory / f"{expected.name}.json"
    try:
        # Even a missing leaf through a substituted parent is not absence proof.
        if directory.resolve() != directory:
            return "unknown"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            return "unknown"
        record = json.loads(path.read_text(encoding="utf-8"))
        process = ExpectedProcess.model_validate(
            {key: record[key] for key in ("pid", "create_time")}
            | {"starttime": record.get("starttime")}
        )
        if process != expected.process or record.get("generation") != expected.generation:
            return "identity_mismatch"
        return "record_present"
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


class ObservationChallenge(EvidenceModel):
    challenge: UUID
    valid_until: AwareDatetime


class ChallengeRequest(EvidenceModel):
    challenge: UUID


class UnitObserver:
    """One prepared request challenge; no registration, readiness or lifecycle API."""

    def __init__(self, expected: ExpectedUnitWriters, challenge: ObservationChallenge) -> None:
        self.expected = expected
        self.challenge = challenge
        self.instance = uuid4()

    async def respond(self, body: bytes) -> tuple[int, bytes, str]:
        try:
            request = ChallengeRequest.model_validate_json(body)
        except ValidationError:
            return 400, b'{"error":"invalid challenge request"}', "application/json"
        if (
            request.challenge != self.challenge.challenge
            or datetime.now(UTC) >= self.challenge.valid_until
        ):
            return 409, b'{"error":"unknown or expired challenge"}', "application/json"
        result = await asyncio.to_thread(self._observe)
        # Observation may block on the OS; expiry applies after collection too.
        if datetime.now(UTC) >= self.challenge.valid_until:
            return 409, b'{"error":"challenge expired during observation"}', "application/json"
        return 200, json.dumps(result).encode(), "application/json"

    def _observe(self) -> dict[str, object]:
        home = Path(self.expected.home)
        if not home.is_absolute() or home.resolve(strict=True) != home:
            raise ValueError("observer requires the existing canonical unit home")
        return {
            "mode": "bootstrap_observation",
            "full_ready": False,
            "challenge": str(self.challenge.challenge),
            "observer_instance": str(self.instance),
            "unit": self.expected.unit().model_dump(mode="json"),
            "observed_at": datetime.now(UTC).isoformat(),
            "processes": [observe_process(item) for item in self.expected.processes],
            "sessions": [observe_session(home, item) for item in self.expected.sessions],
            # Platform producer/observer integration is mandatory; never treat
            # an unimplemented job lookup or empty input as complete closure.
            "launchers": ["unknown" for _item in self.expected.launchers],
            "closure": "unknown",
        }
