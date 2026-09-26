"""The private write-generation ledger under ``$AVA_HOME/db-authority/``.

Settings-free and database-free. ``ledger.json`` records the owner, the
capability groups and every allocated generation; ``generations/<n>.json``
holds that generation's passwords and SCRAM verifiers. The directory is
owner-only (0700), every file 0600, every write atomic with file and directory
fsync. The store sits outside the configuration digest, so a rotation leaves
configuration unchanged.

Transitions take a typed authority token and fail closed:

- ``begin_mint`` publishes the secret file exclusively *before* it records
  ``pending``; a secret file for the next number without a pending record is
  adopted, never regenerated (roles are only created after ``pending`` exists).
- ``activate`` moves ``pending`` to ``active`` only for the exact verified pair.
- ``begin_revoke`` moves the unrevoked generation to ``revoked[revoking]``;
  ``mark_closed`` requires closure evidence covering both logins and then
  deletes the secret file. Nothing moves a number back: ``counter`` never
  decreases and no transition re-admits a revoked generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shared.cluster.authority.model import (
    CLASSES,
    BirthAuthority,
    ClosureEvidence,
    CutoverAuthority,
    Generation,
    GenerationSecret,
    Groups,
    Ledger,
    LedgerRefusedError,
    MintAuthority,
    OperationAuthority,
    Revoked,
    RoleSecret,
    SecretRoles,
    VerifiedGeneration,
    generation_names,
    origin_of,
)
from shared.platform import file_lock
from shared.private_storage import ensure_private_dir, write_private_bytes
from shared.runtime_release import ReleaseRejectedError
from shared.verified_file import regular_bytes

_MAX_FILE_BYTES = 256 * 1024
_SECRET_NAME = re.compile(r"^(0|[1-9][0-9]*)\.json$")
_PARTIAL_SECRET = re.compile(r"^\.(0|[1-9][0-9]*)\.json\.[0-9a-f]{32}\.tmp$")

# (name, password) -> SCRAM verifier; the minting caller supplies it so this
# module never needs a database connection.
Encrypt = Callable[[str, str], str]


def authority_dir(home: Path) -> Path:
    return home / "db-authority"


def ledger_path(home: Path) -> Path:
    return authority_dir(home) / "ledger.json"


def secret_path(home: Path, number: int) -> Path:
    return authority_dir(home) / "generations" / f"{number}.json"


def credential_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _encode(record: Ledger | GenerationSecret) -> bytes:
    return (json.dumps(record.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode()


def _require_private(path: Path, info: os.stat_result, *, directory: bool) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not kind_ok:
        raise LedgerRefusedError(f"database authority path {path} is not a real private node")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
        raise LedgerRefusedError(f"database authority path {path} is not owner-only")


def _read_private(path: Path) -> bytes:
    """Bounded read of an owner-only regular file; FileNotFoundError passes through."""
    _require_private(path, path.lstat(), directory=False)
    try:
        return regular_bytes(path, max_bytes=_MAX_FILE_BYTES)
    except ReleaseRejectedError as exc:
        raise LedgerRefusedError(f"database authority file {path} is not stable: {exc}") from exc


def _secret_numbers(home: Path) -> set[int]:
    directory = authority_dir(home) / "generations"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return set()
    _require_private(directory, info, directory=True)
    numbers: set[int] = set()
    for child in directory.iterdir():
        match = _SECRET_NAME.match(child.name)
        if match is not None:
            numbers.add(int(match.group(1)))
        elif _PARTIAL_SECRET.match(child.name) is None:
            raise LedgerRefusedError(f"unknown file in the database authority store: {child}")
    return numbers


def load_ledger(home: Path) -> Ledger | None:
    """The home's ledger, or None when no authority store has been created.

    A store directory holding secret files without a ledger, a ledger that does
    not parse, one recorded for another home, or any non-private node refuses.
    """
    if not home.is_absolute():
        raise LedgerRefusedError("the database authority home must be absolute")
    directory = authority_dir(home)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    _require_private(directory, info, directory=True)
    try:
        body = _read_private(ledger_path(home))
    except FileNotFoundError:
        if _secret_numbers(home):
            raise LedgerRefusedError("generation secrets exist without a ledger") from None
        return None
    try:
        ledger = Ledger.model_validate_json(body)
    except ValidationError as exc:
        raise LedgerRefusedError(f"database authority ledger is corrupt: {exc}") from exc
    if ledger.home != str(home):
        raise LedgerRefusedError("database authority ledger belongs to another home")
    return ledger


def require_ledger(home: Path) -> Ledger:
    ledger = load_ledger(home)
    if ledger is None:
        raise LedgerRefusedError(
            "no database authority ledger: a new home is born by first start, "
            "an existing home converts through the explicit cutover"
        )
    return ledger


@contextmanager
def _locked(home: Path) -> Generator[None]:
    """Serialize mutations; an existing store must already be private (never repaired)."""
    if not home.is_absolute():
        raise LedgerRefusedError("the database authority home must be absolute")
    directory = authority_dir(home)
    try:
        _require_private(directory, directory.lstat(), directory=True)
    except FileNotFoundError:
        ensure_private_dir(directory)
    with file_lock(directory / "ledger.lock"):
        yield


def _write(home: Path, ledger: Ledger) -> None:
    write_private_bytes(ledger_path(home), _encode(ledger))


def _replace(ledger: Ledger, **updates: Any) -> Ledger:
    return Ledger.model_validate({**dict(ledger), **updates})


def _revoked(entry: Revoked, **updates: Any) -> Revoked:
    return Revoked.model_validate({**dict(entry), **updates})


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_exclusive(path: Path, data: bytes) -> None:
    """Publish ``data`` at ``path`` complete or not at all; never overwrite."""
    directory = ensure_private_dir(path.parent)
    temporary = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)
    _fsync_dir(directory)


def create_ledger(
    home: Path, *, owner: str, groups: Groups, authority: BirthAuthority | CutoverAuthority
) -> Ledger:
    """Create the empty ledger for a birth or cutover; an identical ledger is kept.

    The authority argument is the caller's capability; birth and cutover are the
    only paths that may establish a home's authority store.
    """
    del authority
    with _locked(home):
        existing = load_ledger(home)
        if existing is not None:
            if (existing.owner, existing.groups) != (owner, groups):
                raise LedgerRefusedError("an existing ledger records another owner or other groups")
            return existing
        if _secret_numbers(home):
            raise LedgerRefusedError("generation secrets exist without a ledger")
        ledger = Ledger(version=1, home=str(home), owner=owner, groups=groups)
        _write(home, ledger)
        return ledger


def _require_mint_authority(ledger: Ledger, authority: MintAuthority) -> None:
    if isinstance(authority, OperationAuthority):
        if ledger.counter is None:
            raise LedgerRefusedError("an operation mints only after the home's first generation")
        return
    first = ledger.counter is None or (
        ledger.counter == 0
        and ledger.unrevoked is not None
        and ledger.unrevoked.origin == origin_of(authority)
    )
    if not first:
        raise LedgerRefusedError("birth and cutover authority mint only generation 0")


def _new_secret(home: Path, number: int, encrypt: Encrypt) -> GenerationSecret:
    roles: dict[str, RoleSecret] = {}
    for cls, name in zip(CLASSES, generation_names(number), strict=True):
        password = secrets.token_urlsafe(32)
        roles[cls] = RoleSecret(name=name, password=password, verifier=encrypt(name, password))
    return GenerationSecret(number=number, home=str(home), roles=SecretRoles(**roles))


def _parse_secret(home: Path, number: int, body: bytes) -> GenerationSecret:
    try:
        secret = GenerationSecret.model_validate_json(body)
    except ValidationError as exc:
        raise LedgerRefusedError(f"generation {number} secret is corrupt: {exc}") from exc
    names = (secret.roles.gateway.name, secret.roles.runner.name)
    if (secret.number, secret.home, names) != (number, str(home), generation_names(number)):
        raise LedgerRefusedError(f"generation {number} secret does not match its number or home")
    return secret


def _prepare_store(home: Path, ledger: Ledger, number: int) -> None:
    """Clear crash leftovers and refuse any secret the ledger cannot explain."""
    present = _secret_numbers(home)  # refuses a non-private directory or unknown files
    directory = secret_path(home, number).parent
    if directory.exists():
        for child in directory.iterdir():
            if _PARTIAL_SECRET.match(child.name) is not None:
                child.unlink()  # never published, so never referenced by the ledger
    closed = {entry.number for entry in ledger.revoked if entry.state == "closed"}
    for leftover in sorted(present & closed):
        secret_path(home, leftover).unlink()
    unexplained = present - closed - {number}
    if unexplained:
        raise LedgerRefusedError(f"unexplained generation secrets: {sorted(unexplained)}")


def begin_mint(home: Path, authority: MintAuthority, *, encrypt: Encrypt) -> Generation:
    """Record the next generation as pending, its secret published first.

    An exact retry by the same authority returns the same pending generation.
    Refuses while a generation is active, while a revoked generation is not yet
    closed, or when another authority owns the pending allocation.
    """
    origin = origin_of(authority)
    with _locked(home):
        ledger = require_ledger(home)
        _require_mint_authority(ledger, authority)
        if ledger.pending is not None:
            if ledger.pending.origin != origin:
                raise LedgerRefusedError(
                    f"pending generation {ledger.pending.number} has another origin"
                )
            return ledger.pending
        if ledger.active is not None:
            raise LedgerRefusedError(
                f"generation {ledger.active.number} is active; revoke it first"
            )
        if any(entry.state != "closed" for entry in ledger.revoked):
            raise LedgerRefusedError("a revoked generation is not proven closed")
        number = ledger.next_number
        _prepare_store(home, ledger, number)
        path = secret_path(home, number)
        try:
            body = _read_private(path)
        except FileNotFoundError:
            body = _encode(_new_secret(home, number, encrypt))
            _publish_exclusive(path, body)
        _parse_secret(home, number, body)
        gateway, runner = generation_names(number)
        pending = Generation(
            number=number,
            gateway=gateway,
            runner=runner,
            credential_digest=credential_digest(body),
            origin=origin,
        )
        _write(home, _replace(ledger, counter=number, pending=pending))
        return pending


def read_secret(home: Path, generation: Generation) -> GenerationSecret:
    """The generation's secret, bound to the ledger's credential digest."""
    body = _read_private(secret_path(home, generation.number))
    if credential_digest(body) != generation.credential_digest:
        raise LedgerRefusedError(f"generation {generation.number} secret does not match the ledger")
    return _parse_secret(home, generation.number, body)


def activate(home: Path, authority: MintAuthority, verified: VerifiedGeneration) -> Generation:
    """Admit the exact verified pending generation; re-activation is idempotent.

    The caller performs every activation precondition outside the catalog (the
    pooler userlist and its proof) between ``verify`` and this call.
    """
    origin = origin_of(authority)
    with _locked(home):
        ledger = require_ledger(home)
        current = ledger.unrevoked
        if (
            current is None
            or current.origin != origin
            or (current.number, current.credential_digest, current.roles)
            != (verified.number, verified.credential_digest, verified.roles)
        ):
            raise LedgerRefusedError("activation must name the exact pending generation")
        if ledger.active is not None:
            return ledger.active
        _write(home, _replace(ledger, active=current, pending=None))
        return current


def begin_revoke(home: Path, authority: OperationAuthority) -> tuple[Revoked, ...]:
    """Record the unrevoked generation as revoking; return every revoking entry.

    Idempotent: with no unrevoked generation it records nothing. The authority
    argument is the active operation's capability.
    """
    del authority
    with _locked(home):
        ledger = require_ledger(home)
        current = ledger.unrevoked
        if current is not None:
            entry = Revoked(
                number=current.number,
                gateway=current.gateway,
                runner=current.runner,
                state="revoking",
            )
            revoked = tuple(sorted((*ledger.revoked, entry), key=lambda item: item.number))
            ledger = _replace(ledger, active=None, pending=None, revoked=revoked)
            _write(home, ledger)
        return tuple(entry for entry in ledger.revoked if entry.state == "revoking")


def _delete_closed_secrets(home: Path, ledger: Ledger) -> None:
    closed = {entry.number for entry in ledger.revoked if entry.state == "closed"}
    leftovers = sorted(_secret_numbers(home) & closed)
    for number in leftovers:
        secret_path(home, number).unlink()
    if leftovers:
        _fsync_dir(secret_path(home, leftovers[0]).parent)


def mark_closed(
    home: Path, authority: OperationAuthority, evidence: ClosureEvidence
) -> tuple[Revoked, ...]:
    """Close every revoking generation that ``evidence`` covers, then delete secrets.

    Each revoking generation's logins must be among the census roles of the
    evidence. Returns the entries this call closed; a repeat only finishes
    secret deletion.
    """
    del authority
    with _locked(home):
        ledger = require_ledger(home)
        revoking = {entry.number: entry for entry in ledger.revoked if entry.state == "revoking"}
        uncovered = sorted(
            name
            for entry in revoking.values()
            for name in entry.roles
            if name not in evidence.roles
        )
        if uncovered:
            raise LedgerRefusedError(f"closure evidence does not cover {uncovered}")
        closed = tuple(
            _revoked(entry, state="closed") if entry.number in revoking else entry
            for entry in ledger.revoked
        )
        if revoking:
            ledger = _replace(ledger, revoked=closed)
            _write(home, ledger)
        _delete_closed_secrets(home, ledger)
        return tuple(entry for entry in ledger.revoked if entry.number in revoking)


def record_drops(
    home: Path, authority: OperationAuthority, outcomes: Mapping[int, str | None]
) -> Ledger:
    """Record drop outcomes (``None`` = dropped, else the exact error) for closed entries."""
    del authority
    with _locked(home):
        ledger = require_ledger(home)
        known = {entry.number: entry for entry in ledger.revoked}
        for number in outcomes:
            if number not in known or known[number].state != "closed":
                raise LedgerRefusedError(f"generation {number} is not a closed revoked generation")
        updated = tuple(
            _revoked(
                entry, dropped=outcomes[entry.number] is None, drop_error=outcomes[entry.number]
            )
            if entry.number in outcomes
            else entry
            for entry in ledger.revoked
        )
        ledger = _replace(ledger, revoked=updated)
        _write(home, ledger)
        return ledger
