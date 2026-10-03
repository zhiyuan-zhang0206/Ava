"""The generation's logins in the catalog: mint, exact reconcile and verify.

A generation login is ``LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS`` with a SCRAM verifier computed client-side, owns
nothing, holds no direct grant or role setting, and is a member of exactly its
class group with ``INHERIT TRUE, SET FALSE, ADMIN FALSE``. Its privileges are
therefore exactly the group's.

No code path here sets LOGIN on an existing role. A retry creates only a
missing role of the recorded pending generation; an existing one must match
the secret file exactly or the mint holds.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from psycopg import sql

from base.cluster.authority.catalog import (
    Conn,
    RoleFacts,
    group_members,
    memberships,
    require_admin,
    role_facts,
)
from base.cluster.authority.ledger import begin_mint, read_secret, require_ledger
from base.cluster.authority.model import (
    CLASSES,
    GENERATION_NAMES,
    CatalogRefusedError,
    GenerationSecret,
    Ledger,
    LedgerRefusedError,
    RoleSecret,
    VerifiedGeneration,
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
    """Receipt that the catalog holds exactly the ledger's generation."""
    require_admin(conn)
    ledger = require_ledger(home)
    generation = ledger.generation
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


def mint_generation(conn: Conn, home: Path) -> VerifiedGeneration:
    """Mint, or exactly reconcile, the home's generation and verify it.

    A first mint requires the role names to be absent from the catalog and no
    application login to hold group membership; the secret file and the pending
    record become durable before any role exists. A retry reconciles the
    recorded generation (missing pending roles are created; existing ones must
    match exactly) and never mints another.
    """
    require_admin(conn)
    ledger = require_ledger(home)
    current = ledger.generation
    if current is None:
        collision = sorted(role_facts(conn, GENERATION_NAMES))
        if collision:
            raise CatalogRefusedError((f"roles {collision} already exist before their mint",))
        _require_no_foreign_members(conn, ledger, ())
        current = begin_mint(home, encrypt=partial(scram_verifier, conn))
    _require_no_foreign_members(conn, ledger, current.roles)
    if current != ledger.active:
        # Only a pending generation's roles are ever created; an active
        # generation whose role vanished is an unknown effect and holds below.
        _create_missing(conn, ledger, read_secret(home, current))
    return verify_generation(conn, home)
