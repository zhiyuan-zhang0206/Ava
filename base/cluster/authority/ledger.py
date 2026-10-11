"""The private write-generation ledger under ``$AVA_HOME/db-authority/``.

Settings-free and database-free. ``ledger.json`` records the owner, the
capability groups and every allocated generation; ``generations/<n>.json``
holds that generation's passwords, SCRAM verifiers and machine API tokens. The directory is
owner-only (0700), every file 0600, every write atomic with file and directory
fsync. The store sits outside the configuration digest.

A home has one generation, born by its first start; transitions fail closed:

- ``begin_mint`` publishes the secret file exclusively *before* it records
  ``pending``; a secret file without a pending record is adopted, never
  regenerated (roles are only created after ``pending`` exists), so an
  interrupted birth resumes with the credentials it already published.
- ``activate`` moves ``pending`` to ``active`` only for the exact verified pair,
  after the pooler serves it and both logins answer: no credential is accepted
  or delivered before that.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from base.cluster.authority.model import (
    CLASSES,
    GENERATION_NAMES,
    ApiTokens,
    Generation,
    GenerationSecret,
    Groups,
    Ledger,
    LedgerRefusedError,
    Origin,
    RoleSecret,
    SecretRoles,
    VerifiedGeneration,
)
from base.deploy.release.verified_file import RegularFileReadError, regular_bytes
from base.host.private_storage import ensure_private_dir, write_private_bytes
from base.native_process.os_platform import file_lock

_MAX_FILE_BYTES = 256 * 1024
_SECRET_FILE = "0.json"  # noqa: S105 — a file name, not a credential
_PARTIAL_SECRET = re.compile(r"^\.0\.json\.[0-9a-f]{32}\.tmp$")

# (name, password) -> SCRAM verifier; the minting caller supplies it so this
# module never needs a database connection.
Encrypt = Callable[[str, str], str]


def authority_dir(home: Path) -> Path:
    return home / "db-authority"


def ledger_path(home: Path) -> Path:
    return authority_dir(home) / "ledger.json"


def secret_path(home: Path) -> Path:
    return authority_dir(home) / "generations" / _SECRET_FILE


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
    except RegularFileReadError as exc:
        raise LedgerRefusedError(f"database authority file {path} is not stable: {exc}") from exc


def _secret_published(home: Path) -> bool:
    """Whether the generation's secret file exists; any other file in the store refuses."""
    directory = secret_path(home).parent
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return False
    _require_private(directory, info, directory=True)
    published = False
    for child in directory.iterdir():
        if child.name == _SECRET_FILE:
            published = True
        elif _PARTIAL_SECRET.match(child.name) is None:
            raise LedgerRefusedError(f"unknown file in the database authority store: {child}")
    return published


def load_ledger(home: Path) -> Ledger | None:
    """The home's ledger, or None when no authority store has been created.

    A store directory holding a secret file without a ledger, a ledger that does
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
        if _secret_published(home):
            raise LedgerRefusedError("the generation secret exists without a ledger") from None
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
            "no database authority ledger: a home is born with one by its first start"
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


def create_ledger(home: Path, *, owner: str, groups: Groups) -> Ledger:
    """Create the empty ledger for a birth; an identical ledger is kept.

    Birth is the only path that establishes a home's authority store.
    """
    with _locked(home):
        existing = load_ledger(home)
        if existing is not None:
            if (existing.owner, existing.groups) != (owner, groups):
                raise LedgerRefusedError("an existing ledger records another owner or other groups")
            return existing
        if _secret_published(home):
            raise LedgerRefusedError("the generation secret exists without a ledger")
        ledger = Ledger(version=1, home=str(home), owner=owner, groups=groups)
        _write(home, ledger)
        return ledger


def _new_secret(home: Path, encrypt: Encrypt) -> GenerationSecret:
    roles: dict[str, RoleSecret] = {}
    for cls, name in zip(CLASSES, GENERATION_NAMES, strict=True):
        password = secrets.token_urlsafe(32)
        roles[cls] = RoleSecret(name=name, password=password, verifier=encrypt(name, password))
    api = ApiTokens(gateway=secrets.token_urlsafe(32), runner=secrets.token_urlsafe(32))
    return GenerationSecret(number=0, home=str(home), roles=SecretRoles(**roles), api=api)


def _parse_secret(home: Path, body: bytes) -> GenerationSecret:
    try:
        secret = GenerationSecret.model_validate_json(body)
    except ValidationError as exc:
        raise LedgerRefusedError(f"the generation secret is corrupt: {exc}") from exc
    names = (secret.roles.gateway.name, secret.roles.runner.name)
    if (secret.home, names) != (str(home), GENERATION_NAMES):
        raise LedgerRefusedError("the generation secret does not match its home")
    return secret


def begin_mint(home: Path, *, encrypt: Encrypt) -> Generation:
    """Record the home's generation as pending, its secret published first.

    A retry returns the same pending generation, or records a published secret
    the ledger does not yet name; it never mints another set of credentials.
    Refuses once the generation is active.
    """
    with _locked(home):
        ledger = require_ledger(home)
        if ledger.pending is not None:
            return ledger.pending
        if ledger.active is not None:
            raise LedgerRefusedError("the home's generation is already active")
        _secret_published(home)  # refuses a non-private directory or unknown files
        for child in secret_path(home).parent.glob(".*.tmp"):
            if _PARTIAL_SECRET.match(child.name) is not None:
                child.unlink()  # never published, so never referenced by the ledger
        path = secret_path(home)
        try:
            body = _read_private(path)
        except FileNotFoundError:
            body = _encode(_new_secret(home, encrypt))
            _publish_exclusive(path, body)
        _parse_secret(home, body)
        gateway, runner = GENERATION_NAMES
        pending = Generation(
            number=0,
            gateway=gateway,
            runner=runner,
            credential_digest=credential_digest(body),
            origin=Origin(kind="birth"),
        )
        _write(home, _replace(ledger, pending=pending))
        return pending


def read_secret(home: Path, generation: Generation) -> GenerationSecret:
    """The generation's secret, bound to the ledger's credential digest."""
    body = _read_private(secret_path(home))
    if credential_digest(body) != generation.credential_digest:
        raise LedgerRefusedError("the generation secret does not match the ledger")
    return _parse_secret(home, body)


def activate(home: Path, verified: VerifiedGeneration) -> Generation:
    """Admit the exact verified pending generation; re-activation is idempotent.

    The caller performs every activation precondition outside the catalog (the
    pooler userlist and its proof) between ``verify`` and this call.
    """
    with _locked(home):
        ledger = require_ledger(home)
        current = ledger.generation
        if current is None or (current.number, current.credential_digest, current.roles) != (
            verified.number,
            verified.credential_digest,
            verified.roles,
        ):
            raise LedgerRefusedError("activation must name the exact pending generation")
        if ledger.active is not None:
            return ledger.active
        _write(home, _replace(ledger, active=current, pending=None))
        return current
