"""Exact durable spawn receipts and per-session spawn gates — the fork adjudicator.

A spawn is structurally ambiguous at the boundary between "fork has run" and
"a SessionRecord names the child": a crash inside that window cannot be
resolved by inspecting the record, because the record is the *later* half of
the window. The updater's normal-release chain therefore needs an adjudicator
that can answer, for one exact attempt, "was a process born, and is it still
that same one?" from durable evidence alone. This module is that mechanism:

- **intent** — the spawner atomically writes the attempt's receipt file as
  ``kind="intent"`` BEFORE any lineage process exists (``write_intent``). No
  intent file means the attempt never reached the fork.
- **gate** — the spawner holds a non-blocking exclusive ``flock`` on the
  per-session ``<session>.gate`` file, taken BEFORE the helper is started, and
  passes the descriptor through ``posixproc.new_session`` (``pass_fds``) into
  the helper and hence the child. The lock is held exactly while some process
  of the session's lineage is alive; a free gate proves the lineage is gone.
  Release is ``os.close`` ONLY — ``flock(LOCK_UN)`` would release every
  inherited copy of the shared open file description (including the live
  child's), and ``shared.platform.file_lock`` uses exactly that LOCK_UN in its
  finally clause. Both are banned here for that reason (see take_session_lock
  and probe_session_lock_free).
- **birth** — the child (``shared._reparent``) atomically replaces the intent
  with ``kind="birth"`` carrying its own exact identity (pid, create time,
  Linux ``/proc`` starttime) BEFORE it opens logs or execs. A child that cannot
  write a complete birth receipt exits without exec: the invariant is "no exec
  without an identifiable birth".

``await_birth`` adjudicates one attempt into exactly one of four verdicts —
``spawned_alive`` / ``spawned_dead`` / ``not_spawned`` / ``ambiguous`` — from
durable evidence only: the receipt file, the gate, and a fresh exact process
observation of the receipt's identity. ``ambiguous`` never spawns, kills or
clears anything; it exists so "cannot prove" is representable instead of being
rounded to a guess (the torn-pointer lesson).

Platform rule (design R8): the gated spawn itself requires Linux, where
``/proc`` gives a clock-stable process identity. ``execute_gated_spawn``
refuses on every other platform (fail-fast, like the restricted-hop gate), and
the ``_reparent`` child refuses before exec when it cannot read that identity.
The lock/receipt read path stays platform-neutral so adjudication is testable
and readable everywhere; only the spawn action is platform-gated.

This module is inert until the normal-release chain calls it, and it performs
no database, network or Settings-mutating work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.config import settings
from shared.log import logger
from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.proc_tree import create_time_matches
from shared.session_backend import SessionBackend
from shared.session_record import SessionRecord

# The session-record read ceiling, the same 64 KiB bound `observe_session`
# applies: records are a few hundred bytes; a larger file is not a record.
_RECORD_MAX_BYTES = 64 * 1024

# One session name / generation token: exactly the character class the session
# record layer accepts (ExpectedSession), so a receipt path can never be
# steered out of its directory by the name it embeds.
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

_SPAWN_VERDICTS = ("spawned_alive", "spawned_dead", "not_spawned", "ambiguous")


class SpawnRefusedError(RuntimeError):
    """The spawn must not happen: the session gate is held or the platform has no gated spawn."""


class SpawnNotCompletedError(RuntimeError):
    """Adjudicated ``not_spawned``: the attempt produced no live lineage."""


class SpawnExitedError(RuntimeError):
    """Adjudicated ``spawned_dead``: the exact born child is gone."""


class SpawnAmbiguousError(RuntimeError):
    """The attempt cannot be proven either way; nothing may be spawned, killed or cleared."""


class SpawnEvidenceInvalidError(RuntimeError):
    """Evidence exists but cannot be authenticated — damaged is never absent."""


class _ReceiptReplaced(Exception):  # noqa: N818 — internal control flow, not a verdict
    """The receipt file was replaced while being read; re-read next poll."""


class SpawnExpectation(EvidenceModel):
    """The caller's binding facts for one attempt — what a receipt must restate.

    ``cmd_digest`` is ``sha256`` of the exact launched command string (for
    services: ``"exec " + shlex.join(argv)``), and ``cwd`` the launch
    directory; both are recomputed from the prepared plan, never read back
    from the child.
    """

    nonce: UUID
    session: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    machine: str = Field(min_length=1, max_length=128)
    cmd_digest: Digest
    cwd: str = Field(min_length=1, max_length=4096)


class SpawnReceipt(EvidenceModel):
    """One attempt's durable receipt: written ``intent`` by the spawner, then
    atomically replaced as ``birth`` by the child before it execs.

    A ``birth`` receipt is the only evidence that the child existed, and it
    must be complete: on Linux the ``/proc`` starttime is mandatory, so a birth
    without all four identity fields is a protocol violation, not a weaker
    receipt (the ``_reparent`` child refuses to exec without them).
    """

    version: Literal[1] = 1
    kind: Literal["intent", "birth"]
    nonce: UUID
    session: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    machine: str = Field(min_length=1, max_length=128)
    cmd_digest: Digest
    cwd: str = Field(min_length=1, max_length=4096)
    pid: int | None = Field(default=None, gt=0)
    create_time: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    starttime: int | None = Field(default=None, gt=0)
    captured_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent_kind(self) -> Self:
        identity = (self.pid, self.create_time, self.starttime, self.captured_at)
        if self.kind == "birth":
            if any(field is None for field in identity):
                raise ValueError("birth receipt requires its exact process identity")
        elif any(field is not None for field in identity):
            raise ValueError("intent receipt carries no process identity")
        return self

    def expected_process(self) -> ExpectedProcess:
        """The exact process this receipt names (birth only)."""
        if self.kind != "birth" or self.pid is None or self.create_time is None:
            raise SpawnEvidenceInvalidError("an intent receipt names no process")
        return ExpectedProcess(pid=self.pid, create_time=self.create_time, starttime=self.starttime)

    def matches(self, expectation: SpawnExpectation) -> bool:
        """Whether this receipt binds to the attempt ``expectation`` describes."""
        return (
            self.nonce,
            self.session,
            self.home,
            self.machine,
            self.cmd_digest,
            self.cwd,
        ) == (
            expectation.nonce,
            expectation.session,
            expectation.home,
            expectation.machine,
            expectation.cmd_digest,
            expectation.cwd,
        )


def _checked_name(value: str, what: str) -> str:
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{what} is not a valid session name")
    return value


def spawn_attempt_dir(home: Path, generation: str) -> Path:
    """The per-generation attempt evidence directory ``run/updater-spawn/<generation>``.

    Deliberately NOT under ``run/sessions``: the release inventory scan refuses
    any member of that directory that is not a session ``.json`` record, so
    receipts and gates would break it (_release_inventory.py). The home must be
    absolute: a relative home would silently resolve the receipt path against
    the helper's cwd instead of the unit's.
    """
    root = Path(home)
    if not root.is_absolute():
        raise ValueError("spawn attempt home must be absolute")
    return root / "run" / "updater-spawn" / _checked_name(generation, "generation")


def receipt_path(home: Path, generation: str, session: str, nonce: UUID) -> Path:
    """The per-attempt receipt file: ``<session>.<nonce>.receipt.json``."""
    return (
        spawn_attempt_dir(home, generation)
        / f"{_checked_name(session, 'session')}.{nonce}.receipt.json"
    )


def session_lock_path(home: Path, generation: str, session: str) -> Path:
    """The per-session gate: ``<session>.gate``.

    One inode per (generation, session), shared by every attempt of that
    session — the nonce names the attempt, never the gate (v2/Fix2), so a
    retry takes the same lock and can never slip past a surviving lineage.
    """
    return spawn_attempt_dir(home, generation) / f"{_checked_name(session, 'session')}.gate"


def _fsync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic_text(path: Path, text: str) -> None:
    """Durably publish ``text`` at ``path``: temp + fsync + rename + dir fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        _fsync_parent(path)
    except OSError:
        logger.warning("[spawn-receipt] directory fsync failed after commit", exc_info=True)


def write_intent(path: Path, expectation: SpawnExpectation) -> None:
    """Durably record the attempt BEFORE any lineage process exists.

    The order is the mechanism: a spawn whose intent is not durable must not
    happen, because the receipt file is the recovery path's only handle on the
    attempt. Callers write this first, then take the gate, then start the
    helper.
    """
    intent = SpawnReceipt(kind="intent", **expectation.model_dump())
    _write_atomic_text(path, intent.model_dump_json())


def take_session_lock(path: Path) -> int:
    """Take the per-session spawn gate; the returned descriptor IS the lock.

    Non-blocking: a held gate means some process of this session's lineage is
    alive — a previous unadjudicated attempt, or an external holder, both
    equally conservative inputs — and the caller must refuse rather than queue.

    The descriptor is handed to ``posixproc.new_session`` (``pass_fds``) so the
    helper and the child inherit it across both execs, and it must be released
    by ``os.close`` ONLY:

    - ``flock(LOCK_UN)`` is banned: the lock belongs to the open file
      description, so any holder's unlock releases it for every inherited
      copy — including the live child's.
    - ``shared.platform.file_lock`` is banned: its finally clause is exactly
      that LOCK_UN.

    A gate with no surviving lineage releases itself when the last descriptor
    closes, including when the process crashes.
    """
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise SpawnRefusedError(f"spawn gate is held: {path}") from exc
    return fd


def probe_session_lock_free(path: Path) -> bool:
    """True when no live attempt holds the session gate.

    Proving free means TAKING the gate and releasing it immediately by the
    same close-only rule: the probe descriptor must not survive this call, or
    this process would hold the gate itself afterwards and every later
    adjudication would read "held" against its own probe. An unreadable gate
    path is not free (False) — cannot prove means refuse.
    """
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)


def _receipt_max_bytes() -> int:
    return settings.gateway.update_spawn_receipt_max_bytes


def _gate_poll_seconds() -> float:
    return settings.gateway.update_spawn_gate_poll_seconds


def _bounded_bytes(path: Path, *, limit: int) -> bytes:
    """Read one identity-stable regular file without following a substituted link.

    A file that changes identity while being read is ``_ReceiptReplaced`` — a
    transition (the child replacing intent with birth), not corruption; the
    caller re-reads on its next poll. Damaged bytes are
    ``SpawnEvidenceInvalidError`` (damaged is never absent).
    """
    before = path.lstat()  # FileNotFoundError propagates: the caller decides absence
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise SpawnEvidenceInvalidError("spawn receipt is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SpawnEvidenceInvalidError("spawn receipt is not a regular file")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise _ReceiptReplaced  # noqa: TRY301 — sentinel re-raised for the caller's retry
            body = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except (FileNotFoundError, _ReceiptReplaced) as exc:
        raise _ReceiptReplaced from exc
    except (OSError, ValueError) as exc:
        raise SpawnEvidenceInvalidError("spawn receipt cannot be read safely") from exc
    if len(body) > limit or (opened.st_size, opened.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SpawnEvidenceInvalidError("spawn receipt changed while reading")
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise _ReceiptReplaced
    return body


def _read_receipt_file(path: Path, *, limit: int) -> SpawnReceipt | None:
    """Strict receipt read; None only for a genuinely absent file.

    Exists-but-unreadable or exists-but-not-a-receipt raise
    ``SpawnEvidenceInvalidError`` (never folded to absence), and a replacement
    in flight raises ``_ReceiptReplaced`` (re-read next poll).
    """
    try:
        body = _bounded_bytes(path, limit=limit)
    except FileNotFoundError:
        return None
    try:
        return SpawnReceipt.model_validate_json(body)
    except ValueError as exc:
        raise SpawnEvidenceInvalidError("spawn receipt is malformed") from exc


@dataclass(frozen=True)
class SpawnOutcome:
    """One reconciliation verdict with its evidence and a diagnosable reason."""

    verdict: Literal["spawned_alive", "spawned_dead", "not_spawned", "ambiguous"]
    receipt: SpawnReceipt | None
    reason: str


def _observe_birth(receipt: SpawnReceipt) -> SpawnOutcome:
    """Turn one birth receipt into its exact process verdict."""
    verdict = observe_process(receipt.expected_process())
    if verdict == "alive":
        return SpawnOutcome("spawned_alive", receipt, "the born child is alive")
    if verdict == "exited":
        return SpawnOutcome(
            "spawned_dead", receipt, "record-adjudicated dead: the exact child exited"
        )
    if verdict == "identity_mismatch":
        return SpawnOutcome(
            "spawned_dead",
            receipt,
            "record-adjudicated dead: the pid now holds a different birth",
        )
    return SpawnOutcome("ambiguous", receipt, "the born child's identity cannot be observed")


def _adjudicate_once(
    receipt_file: Path, expectation: SpawnExpectation, lock_path: Path
) -> SpawnOutcome | None:
    """One adjudication pass; None means "not settled yet, keep polling"."""
    try:
        receipt = _read_receipt_file(receipt_file, limit=_receipt_max_bytes())
    except _ReceiptReplaced:
        return None
    except SpawnEvidenceInvalidError as exc:
        return SpawnOutcome("ambiguous", None, f"spawn receipt is not trustworthy: {exc}")
    if receipt is not None and not receipt.matches(expectation):
        return SpawnOutcome(
            "ambiguous", None, "spawn receipt does not bind to this attempt's facts"
        )
    if receipt is not None and receipt.kind == "birth":
        return _observe_birth(receipt)
    # Absent or intent: the gate decides whether any lineage can still exist.
    if probe_session_lock_free(lock_path):
        return SpawnOutcome(
            "not_spawned", receipt, "the session gate is free and no birth receipt exists"
        )
    return None


def await_birth(
    receipt_file: Path,
    expectation: SpawnExpectation,
    lock_path: Path,
    *,
    deadline: float,
    poll_s: float | None = None,
) -> SpawnOutcome:
    """Adjudicate one attempt into its four-value verdict.

    The loop re-reads the receipt each poll: ``intent`` (or absence) with a
    live gate means the child may still be pre-birth; a ``birth`` becomes the
    exact process observation; a free gate proves no lineage survives, so the
    attempt never produced (or already lost) its child. Budget exhaustion is
    ``ambiguous`` — never "assume not_spawned".
    """
    poll = _gate_poll_seconds() if poll_s is None else poll_s
    while True:
        outcome = _adjudicate_once(receipt_file, expectation, lock_path)
        if outcome is not None:
            return outcome
        if time.monotonic() >= deadline:
            return SpawnOutcome(
                "ambiguous",
                None,
                "no decisive spawn evidence within its budget (gate held or receipt in flight)",
            )
        time.sleep(poll)


def execute_gated_spawn(
    backend: SessionBackend,
    *,
    name: str,
    command: str,
    workdir: Path,
    env: dict[str, str],
    home: Path,
    generation: str,
    machine: str,
    nonce: UUID,
    wait_budget: float,
) -> SpawnReceipt:
    """Spawn one exact attempt: gate -> intent -> helper -> birth adjudication.

    The order is the mechanism: take the gate, durably record the intent, only
    then start the helper. The helper's own report (``new_session``'s return
    or exception) is NOT the verdict — the receipt is. A helper that fails
    after forking, or a report lost on the way back, must not become a double
    spawn or a lost child; ``await_birth`` decides from evidence.

    Returns the ``birth`` receipt on ``spawned_alive``; raises
    ``SpawnNotCompletedError`` / ``SpawnExitedError`` / ``SpawnAmbiguousError``
    tagged with the adjudicated verdict otherwise. Refuses up front on any
    platform without Linux ``/proc`` process identity (design R8).
    """
    if sys.platform != "linux":
        raise SpawnRefusedError("gated spawn requires Linux /proc process identity")
    expectation = SpawnExpectation(
        nonce=nonce,
        session=name,
        home=str(home),
        machine=machine,
        cmd_digest=hashlib.sha256(command.encode()).hexdigest(),
        cwd=str(workdir),
    )
    receipt_file = receipt_path(home, generation, name, nonce)
    gate = session_lock_path(home, generation, name)
    fd = take_session_lock(gate)
    failure: BaseException | None = None
    try:
        try:
            write_intent(receipt_file, expectation)
        except OSError as exc:
            raise SpawnNotCompletedError(
                "spawn intent was not durable; nothing was launched"
            ) from exc
        try:
            backend.new_session(
                name,
                command,
                workdir,
                env=env,
                login_shell=False,
                gate_fd=fd,
                receipt=(receipt_file, str(nonce)),
            )
        except BaseException as exc:  # the receipt adjudicates; the report is only a hint
            failure = exc
    finally:
        os.close(fd)
    outcome = await_birth(
        receipt_file,
        expectation,
        gate,
        deadline=time.monotonic() + wait_budget,
    )
    detail = outcome.reason if failure is None else f"{outcome.reason}; helper report: {failure!r}"
    if outcome.verdict == "spawned_alive":
        if failure is not None:
            logger.warning(
                "[spawn-receipt] helper reported {failure!r} but the child is born; adopting the birth receipt",
                failure=failure,
            )
        if outcome.receipt is None:  # unreachable by construction; fail closed
            raise SpawnAmbiguousError(f"spawn verdict is alive without a receipt: {detail}")
        return outcome.receipt
    if outcome.verdict == "not_spawned":
        raise SpawnNotCompletedError(f"spawn produced no live lineage: {detail}")
    if outcome.verdict == "spawned_dead":
        raise SpawnExitedError(f"the spawned child is gone: {detail}")
    raise SpawnAmbiguousError(f"spawn outcome is ambiguous: {detail}")


def read_session_record(home: Path, session: str) -> SessionRecord | None:
    """Read the native session record; damaged is never missing.

    ``SessionRecord.read`` returns None for both a missing file and an
    unreadable one; the adopt path must distinguish them (a missing record may
    be repaired from the birth receipt, a corrupt one must refuse). Same
    discipline as ``observe_session``.
    """
    path = Path(home) / "run" / "sessions" / f"{_checked_name(session, 'session')}.json"
    try:
        body = _bounded_bytes(path, limit=_RECORD_MAX_BYTES)
    except FileNotFoundError:
        return None
    except _ReceiptReplaced as exc:
        raise SpawnEvidenceInvalidError("session record was replaced while reading") from exc
    try:
        return SessionRecord(**json.loads(body))
    except (TypeError, ValueError) as exc:
        raise SpawnEvidenceInvalidError("session record is malformed") from exc


def record_matches_receipt(record: SessionRecord, receipt: SpawnReceipt) -> bool:
    """Cross-check a session record against a birth receipt.

    The stable ``/proc`` starttime is authoritative and compared exactly; only
    a record without it (a legacy/foreign record) falls back to the
    create-time tolerance, the same resolution rule as
    ``SessionRecord.identifies``. Cross-source readings (psutil at spawn vs
    the child's ``/proc`` derivation) may drift within the tolerance by
    construction.
    """
    process = receipt.expected_process()
    if record.pid != process.pid:
        return False
    if record.starttime is not None:
        return record.starttime == process.starttime
    return create_time_matches(record.create_time, process.create_time)


def write_recovered_record(
    home: Path,
    session: str,
    receipt: SpawnReceipt,
    *,
    command: str,
    cwd: Path,
    generation: str,
) -> SessionRecord:
    """The one privileged record repair (the record-missing crash window).

    Reachable only with a birth receipt and a genuinely MISSING record: the
    fields come from the receipt (pid / create_time / starttime / captured_at)
    and the prepared plan (the exact command string and cwd) — never from a
    live process read. The repair then re-reads the record (equality) and
    re-observes the receipt's identity (alive): a failed liveness re-check
    means the child is gone (``SpawnExitedError``), and a corrupt record
    refuses (``SpawnEvidenceInvalidError``) instead of being overwritten. The
    repair is logged with the generation/session/nonce/identity it restored.
    """
    if receipt.kind != "birth":
        raise SpawnEvidenceInvalidError("record repair requires a birth receipt")
    existing = read_session_record(home, session)
    if existing is not None:
        raise SpawnEvidenceInvalidError("record repair refused: a record already exists")
    process = receipt.expected_process()
    if receipt.captured_at is None:  # birth receipts always carry it; fail closed
        raise SpawnEvidenceInvalidError("record repair requires the birth capture time")
    record = SessionRecord(
        pid=process.pid,
        create_time=process.create_time,
        cmd=command,
        cwd=str(cwd),
        started_at=receipt.captured_at.timestamp(),
        starttime=process.starttime,
    )
    record.write(Path(home) / "run" / "sessions" / f"{_checked_name(session, 'session')}.json")
    current = read_session_record(home, session)
    if current != record:
        raise SpawnEvidenceInvalidError("repaired record did not read back equal")
    if observe_process(process) != "alive":
        raise SpawnExitedError("repaired child is no longer alive")
    logger.info(
        "[spawn-receipt] repaired session record from birth receipt "
        "(generation={generation} session={session} nonce={nonce} pid={pid} starttime={starttime})",
        generation=generation,
        session=session,
        nonce=receipt.nonce,
        pid=process.pid,
        starttime=process.starttime,
    )
    return record
