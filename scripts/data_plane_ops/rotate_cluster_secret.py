#!/usr/bin/env python3
"""Rotate the gateway's human cluster secret (the control-plane bearer).

``AVA_CLUSTER_SECRET`` authenticates human and operator callers of the gateway
API and the frontend login. Remote units never hold it: they authenticate with
their write generation's machine API token. Only their telemetry relay token,
derived from this secret, changes with it, so every remote unit needs a newly
issued capability bundle afterwards (``ava cluster db-authority issue-unit``).
The rotation never changes Postgres, Redis, their ACLs or PgBouncer.

Run this script only after a bearer leak. It drives ``advance``, which
journals every step before its effect and never records a secret (only
fingerprints):

1. Stage the next secret in ``backups/secret-rotation/bearer.pending`` (0600).
2. Journal ``pinning``: the old and new bearer fingerprints and the fingerprint
   of the logical-backup passphrase the home keeps.
3. Verify that pin (``services/gateway_side/backup/passphrase``). A home has
   one from its birth, and the secret never touches it; a home without one
   encrypted its earlier logical backups under ``sha256(secret)``, which is
   pinned here so they keep decrypting.
4. Journal ``pinned``. Only then write the new secret into the gateway ``.env``.
5. Journal ``done`` and delete the staged secret.

A crash resumes from the journal: an unpinned passphrase is derived only from
the recorded pre-rotation secret, a pinned one is only verified against its
recorded fingerprint, and the ``.env`` value must be exactly the old or the
staged secret. Restart the gateway afterwards (``ava start``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values

from base.deploy.release.verified_file import regular_bytes
from base.host.env.dotenv_file import upsert_env
from base.host.private_storage import write_private_bytes
from services.gateway_side.backup import passphrase

_TOKEN_BYTES = 32
_SECRET_ENV = "AVA_CLUSTER_SECRET"  # noqa: S105 — env key name, not a credential
_AUDIT = "rotate_cluster_secret"

State = Literal["pinning", "pinned", "done"]


class RotationRefusedError(RuntimeError):
    """The home's bearer, staged secret or pin contradicts the rotation record."""


@dataclass(frozen=True)
class Rotation:
    """One rotation's journal record: fingerprints only, never a secret."""

    state: State
    old: str  # bearer fingerprint of the pre-rotation secret
    new: str  # bearer fingerprint of the staged secret
    pin: str  # fingerprint of the logical-backup passphrase to pin

    @classmethod
    def parse(cls, raw: object) -> Rotation:
        record = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
        state, old, new, pin = (record.get(key) for key in ("state", "old", "new", "pin"))
        if (
            set(record) != {"state", "old", "new", "pin"}
            or state not in ("pinning", "pinned", "done")
            or not (isinstance(old, str) and isinstance(new, str) and isinstance(pin, str))
        ):
            raise RotationRefusedError(f"unrecognized bearer rotation record: {sorted(record)}")
        return cls(state=state, old=old, new=new, pin=pin)


def mint_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def bearer_fingerprint(secret: str) -> str:
    """A journal-safe digest of a bearer, distinct from its derived passphrase."""
    return hashlib.sha256(b"ava-bearer-fingerprint\0" + secret.encode()).hexdigest()


def rotation_dir(home: Path) -> Path:
    return home / "backups" / "secret-rotation"


def pending_path(home: Path) -> Path:
    return rotation_dir(home) / "bearer.pending"


def _current_secret(home: Path) -> str:
    return dotenv_values(home / ".env").get(_SECRET_ENV) or ""


def _staged(home: Path) -> str | None:
    path = pending_path(home)
    return regular_bytes(path).decode().strip() if path.exists() else None


def _begin(home: Path) -> tuple[Rotation, str]:
    """Stage the next secret (a crash-left staged secret is adopted)."""
    current = _current_secret(home)
    if not current:
        raise RotationRefusedError("this home serves an open API (no secret); nothing to rotate")
    staged = _staged(home)
    if staged is None or staged == current:
        staged = mint_secret()
        write_private_bytes(pending_path(home), (staged + "\n").encode())
    pin = passphrase.pinned(home) or passphrase.derive(current)
    rotation = Rotation(
        state="pinning",
        old=bearer_fingerprint(current),
        new=bearer_fingerprint(staged),
        pin=passphrase.fingerprint(pin),
    )
    return rotation, staged


def _pin(home: Path, rotation: Rotation) -> None:
    pinned = passphrase.pinned(home)
    if pinned is None:
        current = _current_secret(home)
        if bearer_fingerprint(current) != rotation.old:
            raise RotationRefusedError(
                "no passphrase is pinned and the .env secret is not the recorded "
                "pre-rotation secret; the passphrase cannot be derived safely"
            )
        pinned = passphrase.derive(current)
        if passphrase.fingerprint(pinned) != rotation.pin:
            raise RotationRefusedError("the pre-rotation passphrase contradicts the record")
        passphrase.pin(home, pinned)
    if passphrase.fingerprint(pinned) != rotation.pin:
        raise RotationRefusedError(f"{passphrase.pin_path(home)} pins another passphrase")


def _write_secret(home: Path, rotation: Rotation) -> None:
    current = bearer_fingerprint(_current_secret(home))
    if current == rotation.new:
        return
    if current != rotation.old:
        raise RotationRefusedError(".env holds neither the recorded old nor the staged secret")
    staged = _staged(home)
    if staged is None or bearer_fingerprint(staged) != rotation.new:
        raise RotationRefusedError(f"{pending_path(home)} is missing or is not the staged secret")
    upsert_env(home / ".env", {_SECRET_ENV: staged}, audit_site=_AUDIT)
    if bearer_fingerprint(_current_secret(home)) != rotation.new:
        raise RuntimeError("the new secret did not read back from .env")


def advance(home: Path, rotation: Rotation | None, save: Callable[[Rotation], None]) -> Rotation:
    """Start (`rotation` None) or continue one rotation to `done`.

    `save` durably records each state before the effect it licenses; a `done`
    rotation is only verified.
    """
    if rotation is None:
        rotation, _staged_secret = _begin(home)
        save(rotation)
    if rotation.state == "pinning":
        _pin(home, rotation)
        rotation = replace(rotation, state="pinned")
        save(rotation)
    if rotation.state == "pinned":
        _pin(home, rotation)  # verify only: never re-derived after `pinning`
        _write_secret(home, rotation)
        rotation = replace(rotation, state="done")
        save(rotation)
    _pin(home, rotation)
    if bearer_fingerprint(_current_secret(home)) != rotation.new:
        raise RotationRefusedError("the recorded rotation is done but .env holds another secret")
    pending_path(home).unlink(missing_ok=True)
    return rotation


# ── the emergency command ────────────────────────────────────────────────────


def journal_path(home: Path) -> Path:
    return rotation_dir(home) / "bearer.json"


def read_journal(home: Path) -> Rotation | None:
    path = journal_path(home)
    if not path.exists():
        return None
    return Rotation.parse(json.loads(regular_bytes(path)))


def _save(home: Path) -> Callable[[Rotation], None]:
    def save(rotation: Rotation) -> None:
        body = json.dumps(asdict(rotation), sort_keys=True) + "\n"
        write_private_bytes(journal_path(home), body.encode())

    return save


def main(argv: list[str] | None = None) -> int:
    from base.paths import ava_home

    parser = argparse.ArgumentParser(description="Rotate the gateway's AVA_CLUSTER_SECRET.")
    parser.add_argument("--execute", action="store_true", help="perform the rotation")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)
    home = ava_home().resolve()
    recorded = read_journal(home)
    resuming = recorded is not None and recorded.state != "done"
    print(f"home: {home}")
    print("scope: the gateway's human bearer; the logical-backup passphrase is pinned first")
    if not args.execute:
        print(f"[dry-run] {'would resume the recorded rotation' if resuming else 'would rotate'}")
        return 0
    if not (args.yes or resuming):
        answer = input("Type 'rotate bearer' to rotate the gateway's cluster secret: ")
        if answer.strip() != "rotate bearer":
            print("aborted.")
            return 1
    try:
        advance(home, recorded if resuming else None, _save(home))
    except (RotationRefusedError, passphrase.PassphrasePinError, OSError) as exc:
        print(f"✗ bearer rotation incomplete; fix the cause and re-run: {exc}", file=sys.stderr)
        return 1
    print("✓ the gateway .env holds the new bearer; the logical-backup passphrase is pinned")
    print(f"  ({passphrase.pin_path(home)} is backup-critical: keep it with the backup keys).")
    print("NEXT: restart the gateway (`ava start`), then issue every remote unit a new")
    print("capability bundle (`ava cluster db-authority issue-unit`): its telemetry token")
    print("derives from the bearer. New browser logins use the new secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
