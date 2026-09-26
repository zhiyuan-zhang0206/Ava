"""Bounded read of the hop ledger — the restart-stable bootstrap recovery slot.

The restricted observer serves this read; an authorized collector recomputes
the payload digest from the returned fields to prove the response is the
slot's own bytes. The reader must stay free of ordinary Settings
(``shared.config``): the restricted entry refuses an observer that imported
them, so it carries its own bounded read (mirroring the writer-side guard in
``shared.updater_handoff``) and imports only config-free evidence modules.

The response is flat: ``mode``, the echoed ``challenge``, ``journal_present`` /
``journal_readable``, the envelope's ``version`` / ``generation`` / ``journal``
and its raw-byte ``payload_digest`` (the last four only when readable),
``boot_id``, and the ops session record's process summary. ``envelope_bytes``
is the canonical serialization the writer stores, so for every legal slot
``sha256(envelope_bytes(version, generation, journal)) == payload_digest``.

The collector's contract with a unit (there is no synchronous write
acknowledgement; design C-3): the served fields bind to the slot's bytes by the
**same envelope's generation** -- ``envelope_bytes`` recomputes the digest over
``version`` + ``generation`` + ``journal`` together, so a response carrying
fields from different writes cannot reproduce ``payload_digest`` -- and the
journal's four digests (``request_digest``, ``inventory_digest``,
``candidate_context_digest``, ``recovery_context_digest``) are recomputed from
the exact bytes the coordinator dispatched and shipped. Response == file bytes,
file bytes == dispatched bytes: nothing in between is trusted as a claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import Field, model_validator

from shared.process_evidence import Digest, EvidenceModel, ExpectedProcess
from shared.updater_recovery import BootstrapRecoveryJournal

LEDGER_MODE = "bootstrap_hop_ledger"

ENVELOPE_VERSION = 1
"""The only recovery envelope version this reader understands (writer-coupled)."""

MAX_LEDGER_BYTES = 256 * 1024
"""Mirror of the writer's bootstrap-recovery budget (``updater_handoff``):
the reader never accepts a slot larger than the writer can legally produce."""

RECOVERY_RELATIVE_PATH = Path("run/updater-bootstrap-recovery.json")
"""The recovery slot relative to the unit home — the writer's exact path."""

SESSION_RELATIVE_PATH = Path("run/sessions/ava-ops.json")
"""The ops session record relative to the unit home (the probe's exact slot)."""

MAX_SESSION_RECORD_BYTES = 64 * 1024
"""Mirror of the observer's session-record read bound (``observe_session``)."""

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
"""Linux's per-boot identity source; platforms without it read as unavailable."""


class LedgerReadError(RuntimeError):
    """The recovery slot is present but is not readable bounded evidence."""


class LedgerRead(EvidenceModel):
    """One bounded read: the envelope's exact fields plus its raw-bytes digest."""

    version: int
    generation: str = Field(min_length=1, max_length=128)
    journal: dict[str, object]
    payload_digest: Digest


SessionRecordState = Literal["ok", "absent", "invalid"]


class SessionRecordSummary(EvidenceModel):
    """The ops session record's process identity, never its whole provenance.

    ``absent`` (no record file) and ``invalid`` (present but unreadable) stay
    distinct — neither proves a responder.
    """

    state: SessionRecordState
    pid: int | None = Field(default=None, gt=0)
    create_time: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    starttime: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def coherent_identity(self) -> Self:
        identity = (self.pid, self.create_time, self.starttime)
        if self.state == "ok":
            if self.pid is None or self.create_time is None:
                raise ValueError("an ok session summary requires its process identity")
        elif any(value is not None for value in identity):
            raise ValueError("a non-ok session summary carries no process identity")
        return self


def envelope_bytes(version: int, generation: str, journal: dict[str, object]) -> bytes:
    """The exact canonical bytes the recovery writer stores for one envelope.

    Recomputing ``sha256(envelope_bytes(...))`` from a served response and
    comparing it with ``payload_digest`` proves the response equals the bytes
    the slot held at read time: the writer stores this same serialization
    (sorted keys, compact separators, ASCII-escaped).
    """
    return json.dumps(
        {"version": version, "generation": generation, "journal": journal},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def read_recovery_ledger(path: Path) -> LedgerRead | None:
    """One bounded read of the recovery slot; ``None`` only for a positively absent file.

    Every other unreadable or damaged state raises ``LedgerReadError`` — never
    a silent absence; the route encodes that as ``journal_readable=false``.
    """
    try:
        raw = _bounded_bytes(path, limit=MAX_LEDGER_BYTES)
    except FileNotFoundError:
        return None
    return _parse_ledger(raw)


def _parse_ledger(raw: bytes) -> LedgerRead:
    try:
        parsed: object = json.loads(raw)
    except ValueError as exc:
        raise LedgerReadError("the recovery slot is not a JSON envelope") from exc
    if not isinstance(parsed, dict):
        raise LedgerReadError("the recovery slot is not a JSON envelope")
    envelope = cast("dict[str, object]", parsed)
    if set(envelope) != {"version", "generation", "journal"}:
        raise LedgerReadError("the recovery envelope does not carry its exact fields")
    version = envelope["version"]
    generation = envelope["generation"]
    journal = envelope["journal"]
    if isinstance(version, bool) or not isinstance(version, int) or version != ENVELOPE_VERSION:
        raise LedgerReadError("the recovery envelope version is unsupported")
    if not isinstance(generation, str) or not generation or len(generation) > 128:
        raise LedgerReadError("the recovery generation is malformed")
    if not isinstance(journal, dict):
        raise LedgerReadError("the recovery journal is malformed")
    try:
        BootstrapRecoveryJournal.model_validate_json(json.dumps(journal))
    except ValueError as exc:
        raise LedgerReadError("the recovery journal does not parse") from exc
    return LedgerRead(
        version=version,
        generation=generation,
        journal=cast("dict[str, object]", journal),
        payload_digest=hashlib.sha256(raw).hexdigest(),
    )


def _bounded_bytes(path: Path, *, limit: int) -> bytes:
    """One identity-stable read of an owned regular file, never following a link.

    Mirrors the writer-side guard in ``shared.updater_handoff`` (this module
    cannot import it: it loads ordinary Settings). ``FileNotFoundError`` of the
    path itself propagates — the caller owns the absent/present distinction.
    """
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit or before.st_uid != os.getuid():
        raise LedgerReadError("the recovery slot is not an owned bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise LedgerReadError("the recovery slot changed while opening")
            body = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError as exc:
        raise LedgerReadError("the recovery slot cannot be read safely") from exc
    if (
        len(body) > limit
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise LedgerReadError("the recovery slot changed while reading")
    return body


def read_boot_id(path: Path = BOOT_ID_PATH) -> UUID | None:
    """The unit's per-boot identity, or ``None`` when no source is available.

    Linux supplies a UUID at ``/proc/sys/kernel/random/boot_id``; the restricted
    hop that writes the slot only runs on Linux, so a served ledger there always
    carries one, while other platforms read as unavailable (the collector
    refuses either way).
    """
    try:
        return UUID(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def read_session_record(home: Path) -> SessionRecordSummary:
    """Summarize the ops session record's process identity, the probe's own slot.

    This mirrors ``observe_session``'s read discipline: a substituted parent is
    never absence, and a record that cannot be read as an exact process identity
    is ``invalid``, not silently dropped.
    """
    directory = home / "run" / "sessions"
    path = directory / "ava-ops.json"
    try:
        if directory.resolve() != directory:
            return SessionRecordSummary(state="invalid")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SESSION_RECORD_BYTES:
            return SessionRecordSummary(state="invalid")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SessionRecordSummary(state="absent")
    except (OSError, ValueError):
        return SessionRecordSummary(state="invalid")
    try:
        process = ExpectedProcess.model_validate(
            {
                "pid": record["pid"],
                "create_time": record["create_time"],
                "starttime": record.get("starttime"),
            }
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return SessionRecordSummary(state="invalid")
    return SessionRecordSummary(
        state="ok",
        pid=process.pid,
        create_time=process.create_time,
        starttime=process.starttime,
    )


def build_ledger_payload(home: Path, challenge: UUID) -> dict[str, object]:
    """One flat read of every ledger fact; a damaged slot stays encoded, not raised.

    ``journal_present`` is false only for a positively absent file; every other
    failure reads as ``journal_readable=false`` — the collector refuses either
    way without the read itself failing the operation.
    """
    try:
        recovery: LedgerRead | None = read_recovery_ledger(home / RECOVERY_RELATIVE_PATH)
        unreadable = False
    except LedgerReadError:
        recovery = None
        unreadable = True
    boot_id = read_boot_id()
    payload: dict[str, object] = {
        "mode": LEDGER_MODE,
        "challenge": str(challenge),
        "journal_present": recovery is not None or unreadable,
        "journal_readable": recovery is not None,
        "boot_id": None if boot_id is None else str(boot_id),
        "session_record": read_session_record(home).model_dump(mode="json"),
    }
    if recovery is not None:
        payload.update(
            version=recovery.version,
            generation=recovery.generation,
            journal=recovery.journal,
            payload_digest=recovery.payload_digest,
        )
    return payload
