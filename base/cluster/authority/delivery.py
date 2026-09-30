"""Deliver the home's write generation: pooler userlist, launch grants, CLI use.

Settings-free and database-free. Every consumer of a generation's credentials
reads them through here, bound to the ledger's credential digest:

- the owned PgBouncer, whose ``auth_file`` holds exactly one generation's two
  SCRAM verifiers plus the operator admin-console entry ``ava_pooler_admin``
  (a userlist-only name, never a PostgreSQL role);
- the root launcher, which projects one class login (and its API token) into
  one service's launch environment (``write_grant``); only ``number`` and
  ``credential_digest`` reach launch digests and journals;
- an operator process on the gateway home that no launcher injected
  (``consume``, as environment ``operator_environment``), admitted only while
  it runs the home's admitted runtime: the source checkout the home was born from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.cluster.authority.ledger import (
    Encrypt,
    _locked,
    _publish_exclusive,
    _read_private,
    authority_dir,
    read_secret,
    require_ledger,
)
from base.cluster.authority.model import (
    AuthorityRefusedError,
    Generation,
    GenerationClass,
    LedgerRefusedError,
    Verifier,
)

POOLER_ADMIN = "ava_pooler_admin"
# Non-secret launch-environment marker naming the delivered generation; its
# presence is what makes a launcher-injected AVA_DB_URL authoritative.
GENERATION_ENV = "AVA_DB_GENERATION"


class PoolerAdmin(BaseModel):
    """``pooler-admin.json``: the PgBouncer admin-console credential (0600)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    password: str = Field(min_length=32, max_length=256, repr=False)
    verifier: Verifier


def pooler_admin_path(home: Path) -> Path:
    return authority_dir(home) / "pooler-admin.json"


def _parse_pooler_admin(body: bytes) -> PoolerAdmin:
    try:
        return PoolerAdmin.model_validate_json(body)
    except ValidationError as exc:
        raise LedgerRefusedError(f"pooler admin credential is corrupt: {exc}") from exc


def read_pooler_admin(home: Path) -> PoolerAdmin:
    """The admin-console credential; missing or loose files refuse."""
    try:
        return _parse_pooler_admin(_read_private(pooler_admin_path(home)))
    except FileNotFoundError:
        raise LedgerRefusedError("no pooler admin credential: birth creates it") from None


def ensure_pooler_admin(home: Path, *, encrypt: Encrypt) -> PoolerAdmin:
    """Create the admin-console credential once, published exclusively.

    Birth calls this; an existing credential is kept, never rotated, so a
    retry reuses the value the running pooler already holds.
    """
    import secrets

    with _locked(home):
        path = pooler_admin_path(home)
        if path.exists():
            return _parse_pooler_admin(_read_private(path))
        password = secrets.token_urlsafe(32)
        admin = PoolerAdmin(password=password, verifier=encrypt(POOLER_ADMIN, password))
        _publish_exclusive(path, (admin.model_dump_json() + "\n").encode())
        return _parse_pooler_admin(_read_private(path))


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def render_userlist(home: Path, generation: Generation) -> bytes:
    """``auth_file`` bytes: ``generation``'s two verifiers and the admin console.

    Deterministic from the secret file and the admin credential, so a retry
    renders identical bytes and an unchanged pooler is never restarted.
    """
    secret = read_secret(home, generation)
    admin = read_pooler_admin(home)
    entries = [
        (secret.roles.gateway.name, secret.roles.gateway.verifier),
        (secret.roles.runner.name, secret.roles.runner.verifier),
        (POOLER_ADMIN, admin.verifier),
    ]
    return "".join(f"{_quoted(name)} {_quoted(value)}\n" for name, value in entries).encode()


@dataclass(frozen=True)
class WriteGrant:
    """One class login and API token of the home's active generation, for one launch."""

    number: int
    credential_digest: str
    role: str
    password: str = field(repr=False)
    api_token: str = field(repr=False)

    def dsn(self, endpoint: str) -> str:
        """``endpoint`` (the home's credential-free URL) dialed as this login."""
        from base.host.net.url_secret import url_with_userinfo

        return url_with_userinfo(endpoint, self.role, self.password)

    def environment(self, endpoint: str, *, api: bool) -> dict[str, str]:
        """The process-environment delivery: the login, its generation marker and,
        when the cluster's API is authenticated (``api``), the class API token."""
        from base.cluster.authority.api import API_TOKEN_ENV

        delivered = {"AVA_DB_URL": self.dsn(endpoint), GENERATION_ENV: str(self.number)}
        return {**delivered, API_TOKEN_ENV: self.api_token} if api else delivered

    @property
    def reference(self) -> dict[str, object]:
        """The only facts about a grant that launch proofs and journals carry."""
        return {"number": self.number, "credential_digest": self.credential_digest}


def active_generation(home: Path) -> Generation:
    """The ledger's active generation; a home without one cannot deliver."""
    ledger = require_ledger(home)
    if ledger.active is None:
        raise LedgerRefusedError(
            "the database authority has no active generation; a pending or revoked "
            "generation is never delivered"
        )
    return ledger.active


def write_grant(home: Path, cls: GenerationClass) -> WriteGrant:
    """The active generation's ``cls`` login, bound to the ledger digest."""
    generation = active_generation(home)
    secret = read_secret(home, generation)
    role = secret.roles.of(cls)
    return WriteGrant(
        number=generation.number,
        credential_digest=generation.credential_digest,
        role=role.name,
        password=role.password,
        api_token=secret.api.of(cls),
    )


def _intent_checkout(home: Path) -> Path:
    from base.deploy.release.verified_file import regular_bytes

    try:
        data: object = json.loads(regular_bytes(home / "start-intent.json", max_bytes=1 << 20))
    except (OSError, ValueError) as exc:
        raise AuthorityRefusedError(f"the home's start intent is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthorityRefusedError("the home's start intent is malformed")
    fields: dict[str, object] = {str(key): value for key, value in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType] — JSON object
    checkout = fields.get("checkout")
    if fields.get("home") != str(home) or not isinstance(checkout, str):
        raise AuthorityRefusedError("the home's start intent names another home or no checkout")
    return Path(checkout)


def require_admitted_runtime(home: Path, *, code_root: Path) -> None:
    """Refuse unless ``code_root`` is the home's admitted runtime.

    Only the source checkout recorded by the home's start intent is admitted;
    a job still pointing at another checkout receives nothing.
    """
    checkout = _intent_checkout(home)
    if code_root.resolve() != checkout.resolve():
        raise AuthorityRefusedError(
            f"this process runs {code_root}, not the home's source checkout {checkout}"
        )


def consume(home: Path, cls: GenerationClass) -> WriteGrant:
    """The active ``cls`` login for a process the launcher did not inject.

    Admission is the loaded runtime (``require_admitted_runtime``); any refusal
    raises ``AuthorityRefusedError`` and delivers nothing.
    """
    code_root = Path(__file__).resolve().parents[3]
    require_admitted_runtime(home, code_root=code_root)
    return write_grant(home, cls)


def operator_environment(
    home: Path, endpoint: str, *, launcher: str | None, api: bool
) -> dict[str, str]:
    """The environment delivery of an operator process on the gateway ``home``.

    Processes the root launcher starts carry their class login in the launch
    environment. An operator process (the ``ava`` CLI, a script, an OS job)
    that no launcher started (``launcher``, the launcher profile its process
    tree descends from, is None) receives the active gateway login dialing
    ``endpoint`` (and, when ``api``, the gateway API token) only while it runs
    the home's admitted runtime (``consume``).

    Raises:
        AuthorityRefusedError: why this process receives nothing.
    """
    if launcher is not None:
        raise AuthorityRefusedError(
            f"this {launcher}-profile process was launched without a delivered write "
            "generation; only the root launcher delivers database logins"
        )
    try:
        grant = consume(home, "gateway")
    except (AuthorityRefusedError, ValueError, OSError) as exc:
        raise AuthorityRefusedError(f"no database authority for this process: {exc}") from exc
    return grant.environment(endpoint, api=api)
