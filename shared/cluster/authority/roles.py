"""Generation logins in the catalog: mint, exact reconcile, sweep and prune.

A generation login is ``LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS`` with a SCRAM verifier computed client-side, owns
nothing, holds no direct grant or role setting, and is a member of exactly its
class group with ``INHERIT TRUE, SET FALSE, ADMIN FALSE``. Its privileges are
therefore exactly the group's.

No code path here sets LOGIN on an existing role. A retry creates only a
missing role of the recorded pending generation; an existing one must match
the secret file exactly or the mint holds. The sweep and prune only remove.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import psycopg
from psycopg import sql

from shared.cluster.authority.catalog import (
    BOOTSTRAP_SUPERUSER_OID,
    Conn,
    RoleFacts,
    group_members,
    memberships,
    require_admin,
    role_facts,
)
from shared.cluster.authority.ledger import (
    begin_mint,
    read_secret,
    record_drops,
    require_ledger,
)
from shared.cluster.authority.model import (
    CLASSES,
    CatalogRefusedError,
    Generation,
    GenerationSecret,
    Ledger,
    LedgerRefusedError,
    MintAuthority,
    OperationAuthority,
    Revoked,
    RoleSecret,
    VerifiedGeneration,
    generation_names,
    origin_of,
)


def scram_verifier(conn: Conn, name: str, password: str) -> str:
    """SCRAM-SHA-256 verifier computed by libpq on the client; plaintext never
    reaches SQL text or the server log."""
    return conn.pgconn.encrypt_password(password.encode(), name.encode(), b"scram-sha-256").decode()


def _shape_violations(fact: RoleFacts, verifier: str) -> list[str]:
    name = fact.name
    violations: list[str] = []
    if not fact.login:
        violations.append(f"{name} cannot log in")
    if fact.elevated:
        violations.append(f"{name} holds {', '.join(fact.elevated)}")
    if not fact.inherit:
        violations.append(f"{name} is NOINHERIT")
    if fact.connection_limit != -1 or fact.valid_until is not None:
        violations.append(f"{name} carries a connection limit or expiry")
    if fact.password != verifier:
        violations.append(f"{name} stored verifier differs from its secret")
    if fact.configured:
        violations.append(f"{name} carries role settings")
    if fact.depended:
        violations.append(f"{name} owns objects or holds direct grants")
    return violations


def login_violations(conn: Conn, secret: RoleSecret, group: str) -> list[str]:
    """Why ``secret.name`` is not exactly a generation login of ``group``."""
    fact = role_facts(conn, (secret.name,)).get(secret.name)
    if fact is None:
        return [f"{secret.name} does not exist"]
    violations = _shape_violations(fact, secret.verifier)
    rows = memberships(conn, (secret.name,))
    held = [
        (row.role, row.admin, row.inherit, row.set) for row in rows if row.member == secret.name
    ]
    if held != [(group, False, True, False)]:
        violations.append(f"{secret.name} memberships {held} differ from INHERIT-only {group}")
    if any(row.role == secret.name for row in rows):
        violations.append(f"{secret.name} has members")
    return violations


def verify_generation(conn: Conn, home: Path) -> VerifiedGeneration:
    """Receipt that the catalog holds exactly the ledger's unrevoked generation."""
    require_admin(conn)
    ledger = require_ledger(home)
    generation = ledger.unrevoked
    if generation is None:
        raise LedgerRefusedError("no active or pending generation to verify")
    secret = read_secret(home, generation)
    violations: list[str] = []
    for cls in CLASSES:
        violations += login_violations(conn, secret.roles.of(cls), ledger.groups.of(cls))
    if violations:
        raise CatalogRefusedError(tuple(violations))
    return VerifiedGeneration(
        number=generation.number,
        credential_digest=generation.credential_digest,
        roles=generation.roles,
    )


def _require_no_foreign_members(conn: Conn, ledger: Ledger, allowed: tuple[str, ...]) -> None:
    foreign = sorted(set(group_members(conn, ledger.groups)) - set(allowed))
    if foreign:
        raise CatalogRefusedError(
            (f"application logins outside the minting generation hold group membership: {foreign}",)
        )


def _create_missing(conn: Conn, ledger: Ledger, secret: GenerationSecret) -> None:
    with conn.transaction():
        existing = role_facts(conn, (secret.roles.gateway.name, secret.roles.runner.name))
        for cls in CLASSES:
            role = secret.roles.of(cls)
            if role.name in existing:
                continue
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role.name), sql.Literal(role.verifier))
            )
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
                    sql.Identifier(ledger.groups.of(cls)), sql.Identifier(role.name)
                )
            )


def mint_generation(conn: Conn, home: Path, authority: MintAuthority) -> VerifiedGeneration:
    """Mint, or exactly reconcile, the authority's generation and verify it.

    A fresh number requires its role names to be absent from the catalog and no
    other application login to hold group membership; the secret file and the
    pending record become durable before any role exists. A retry of the same
    authority reconciles the recorded generation (missing pending roles are
    created; existing ones must match exactly) and never allocates another.
    """
    require_admin(conn)
    ledger = require_ledger(home)
    current: Generation | None = ledger.unrevoked
    if current is not None and current.origin != origin_of(authority):
        raise LedgerRefusedError(f"generation {current.number} belongs to another authority")
    if current is None:
        names = generation_names(ledger.next_number)
        collision = sorted(role_facts(conn, names))
        if collision:
            raise CatalogRefusedError((f"roles {collision} already exist before their mint",))
        _require_no_foreign_members(conn, ledger, ())
        current = begin_mint(home, authority, encrypt=partial(scram_verifier, conn))
    _require_no_foreign_members(conn, ledger, current.roles)
    if current != ledger.active:
        # Only a pending generation's roles are ever created; an active
        # generation whose role vanished is an unknown effect and holds below.
        _create_missing(conn, ledger, read_secret(home, current))
    return verify_generation(conn, home)


def _stale_logins(conn: Conn, ledger: Ledger) -> tuple[str, ...]:
    """Every application login except the unrevoked generation's.

    Recorded revoked names are included whether or not the role exists: a
    pending generation revoked before its roles were created must still be
    covered by closure evidence, and a restored catalog may recreate a name.
    """
    keep: set[str] = set(ledger.unrevoked.roles) if ledger.unrevoked is not None else set()
    recorded = {name for entry in ledger.revoked for name in entry.roles}
    members = set(group_members(conn, ledger.groups))
    return tuple(sorted((recorded | members) - keep))


def fenced_roles(conn: Conn, home: Path) -> tuple[str, ...]:
    """The roles a fence closes: stale application logins plus owner and groups.

    Owner and groups are included so a legacy session (from before the cutover,
    or resurrected by a restored catalog) is part of every closure census.
    """
    ledger = require_ledger(home)
    legacy = (ledger.owner, ledger.groups.gateway, ledger.groups.runner)
    return tuple(sorted({*_stale_logins(conn, ledger), *legacy}))


@dataclass(frozen=True)
class SweepResult:
    demoted: tuple[str, ...]
    memberships_revoked: tuple[tuple[str, str], ...]


def sweep(conn: Conn, home: Path) -> SweepResult:
    """Monotone NOLOGIN sweep of every stale application login, in one transaction.

    Read-only on the ledger. Demotes to NOLOGIN without a password every
    recorded non-active generation login, every other member of either group,
    and the owner and groups themselves, then revokes those roles' group
    memberships (by their recorded grantor). The unrevoked generation is left
    untouched. The bootstrap superuser can never be an application role.
    """
    require_admin(conn)
    ledger = require_ledger(home)
    groups = (ledger.groups.gateway, ledger.groups.runner)
    demoted: list[str] = []
    revoked: list[tuple[str, str]] = []
    with conn.transaction():
        stale = _stale_logins(conn, ledger)
        facts = role_facts(conn, (*stale, ledger.owner, *groups))
        for name, fact in sorted(facts.items()):
            if fact.oid == BOOTSTRAP_SUPERUSER_OID:
                raise CatalogRefusedError((f"bootstrap superuser {name} is an application role",))
            if fact.login or fact.password is not None:
                conn.execute(
                    sql.SQL("ALTER ROLE {} NOLOGIN PASSWORD NULL").format(sql.Identifier(name))
                )
                demoted.append(name)
        for row in memberships(conn, stale):
            if row.member in stale and row.role in groups:
                conn.execute(
                    sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
                        sql.Identifier(row.role),
                        sql.Identifier(row.member),
                        sql.Identifier(row.grantor),
                    )
                )
                revoked.append((row.role, row.member))
    return SweepResult(demoted=tuple(demoted), memberships_revoked=tuple(revoked))


@dataclass(frozen=True)
class PruneResult:
    dropped: tuple[str, ...]
    retained: tuple[tuple[str, str], ...]


def _drop(conn: Conn, name: str) -> str | None:
    try:
        with conn.transaction():
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
    except psycopg.errors.DependentObjectsStillExist as exc:
        detail = exc.diag.message_detail or ""
        return f"{exc.diag.message_primary}: {detail}".strip()
    return None


def _prune_entry(
    conn: Conn, entry: Revoked, existing: Collection[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    dropped: list[str] = []
    retained: list[tuple[str, str]] = []
    for name in entry.roles:
        if name not in existing:
            continue  # an earlier prune dropped it before recording the outcome
        error = _drop(conn, name)
        if error is None:
            dropped.append(name)
        else:
            retained.append((name, error))
    return dropped, retained


def prune(conn: Conn, home: Path, authority: OperationAuthority) -> PruneResult:
    """Drop every closed, revoked login, one statement each; keep inert failures.

    Never ``DROP OWNED`` or ``CASCADE``: a role that still has dependencies
    stays as a NOLOGIN tombstone and its exact error is recorded, then retried
    on the next prune. A revoked role that regained LOGIN refuses (sweep first).
    """
    require_admin(conn)
    ledger = require_ledger(home)
    targets = [entry for entry in ledger.revoked if entry.state == "closed" and not entry.dropped]
    existing = role_facts(conn, [name for entry in targets for name in entry.roles])
    relogin = sorted(name for name, fact in existing.items() if fact.login or fact.password)
    if relogin:
        raise CatalogRefusedError((f"revoked roles regained LOGIN: {relogin}",))
    dropped: list[str] = []
    retained: list[tuple[str, str]] = []
    outcomes: dict[int, str | None] = {}
    for entry in targets:
        entry_dropped, entry_retained = _prune_entry(conn, entry, existing)
        dropped += entry_dropped
        retained += entry_retained
        errors = "; ".join(f"{name}: {error}" for name, error in entry_retained)
        outcomes[entry.number] = errors or None
    if outcomes:
        record_drops(home, authority, outcomes)
    return PruneResult(dropped=tuple(dropped), retained=tuple(retained))
