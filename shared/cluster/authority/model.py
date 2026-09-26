"""Typed records, authority tokens, receipts and refusals for write generations.

Everything here is data: the ledger and secret-file shapes, the tokens a caller
presents to mutate the ledger, and the receipts only the catalog and closure
code construct. Names are recorded data and are never parsed back into
authority; a role name prefix grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GenerationClass = Literal["gateway", "runner"]
CLASSES: tuple[GenerationClass, ...] = ("gateway", "runner")
Direction = Literal["candidate", "previous"]

# Stable NOLOGIN capability groups. `ava_runner` is the historical runner login,
# demoted in place so its existing grants keep working.
GATEWAY_GROUP = "ava_gateway"
RUNNER_GROUP = "ava_runner"

RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Verifier = Annotated[str, StringConstraints(pattern=r"^SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$")]


class AuthorityRefusedError(RuntimeError):
    """Hold: the authority state is not one this library may act on."""


class LedgerRefusedError(AuthorityRefusedError):
    """The private ledger or a secret file is missing, corrupt or not private,
    or the requested transition is not permitted from its recorded state."""


class CatalogRefusedError(AuthorityRefusedError):
    """The PostgreSQL catalog holds an unknown or contradicting effect."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__("database authority catalog refused: " + "; ".join(violations))


def generation_names(number: int) -> tuple[str, str]:
    """The gateway and runner login names minted for ``number``."""
    return f"ava_g{number}_gateway", f"ava_g{number}_runner"


@dataclass(frozen=True)
class BirthAuthority:
    """Granted by first-start initialization under its start-intent lock."""


@dataclass(frozen=True)
class CutoverAuthority:
    """Granted by the one-time explicit cutover of an existing home."""


@dataclass(frozen=True)
class OperationAuthority:
    """Granted by the home's active finite operation under its operation lock."""

    operation: UUID
    direction: Direction


MintAuthority = BirthAuthority | CutoverAuthority | OperationAuthority


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Groups(_Record):
    gateway: RoleName
    runner: RoleName

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if self.gateway == self.runner:
            raise ValueError("capability groups must be distinct roles")
        return self

    def of(self, cls: GenerationClass) -> str:
        return self.gateway if cls == "gateway" else self.runner


class Origin(_Record):
    kind: Literal["birth", "cutover", "operation"]
    operation: str | None = None
    direction: Direction | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.kind == "operation") != (self.operation is not None):
            raise ValueError("only an operation origin names its operation")
        if (self.kind == "operation") != (self.direction is not None):
            raise ValueError("only an operation origin names its direction")
        if self.operation is not None and str(UUID(self.operation)) != self.operation:
            raise ValueError("operation origin requires a canonical UUID")
        return self


def origin_of(authority: MintAuthority) -> Origin:
    """The ledger origin recorded for a mint under ``authority``."""
    if isinstance(authority, BirthAuthority):
        return Origin(kind="birth")
    if isinstance(authority, CutoverAuthority):
        return Origin(kind="cutover")
    return Origin(
        kind="operation", operation=str(authority.operation), direction=authority.direction
    )


class Generation(_Record):
    number: int = Field(ge=0)
    gateway: RoleName
    runner: RoleName
    credential_digest: Digest
    origin: Origin

    @property
    def roles(self) -> tuple[str, str]:
        return self.gateway, self.runner

    def role(self, cls: GenerationClass) -> str:
        return self.gateway if cls == "gateway" else self.runner


class Revoked(_Record):
    number: int = Field(ge=0)
    gateway: RoleName
    runner: RoleName
    state: Literal["revoking", "closed"]
    dropped: bool = False
    drop_error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.dropped and (self.state != "closed" or self.drop_error is not None):
            raise ValueError("only a closed generation drops, and a drop leaves no error")
        if self.drop_error is not None and self.state != "closed":
            raise ValueError("drop errors belong to closed generations")
        return self

    @property
    def roles(self) -> tuple[str, str]:
        return self.gateway, self.runner


class Ledger(_Record):
    """The home's write-generation authority record.

    Every number in ``0..counter`` is exactly one of: the active generation,
    the pending generation, or a revoked generation. Only the unrevoked
    generation may hold LOGIN; a lower number is revoked by definition.
    """

    version: Literal[1]
    home: str
    owner: RoleName
    groups: Groups
    counter: int | None = Field(default=None, ge=0)
    active: Generation | None = None
    pending: Generation | None = None
    revoked: tuple[Revoked, ...] = ()

    @model_validator(mode="after")
    def numbers_accounted(self) -> Self:
        if self.active is not None and self.pending is not None:
            raise ValueError("a pending generation exists only while none is active")
        numbers = [entry.number for entry in self.revoked]
        if numbers != sorted(set(numbers)):
            raise ValueError("revoked generations must be unique and ordered")
        if self.unrevoked is not None:
            numbers.append(self.unrevoked.number)
        expected = [] if self.counter is None else list(range(self.counter + 1))
        if sorted(numbers) != expected:
            raise ValueError("every allocated generation number must be recorded exactly once")
        if self.pending is not None and self.pending.number != self.counter:
            raise ValueError("a pending generation is always the latest allocation")
        return self

    @model_validator(mode="after")
    def names_distinct(self) -> Self:
        entries = [*self.revoked, *([self.unrevoked] if self.unrevoked is not None else [])]
        names = [name for entry in entries for name in entry.roles]
        reserved = {self.owner, self.groups.gateway, self.groups.runner}
        if len(names) != len(set(names)) or reserved & set(names):
            raise ValueError("generation logins must be unique and distinct from owner and groups")
        if len(reserved) != 3:
            raise ValueError("the schema owner is not a capability group")
        return self

    @property
    def next_number(self) -> int:
        return 0 if self.counter is None else self.counter + 1

    @property
    def unrevoked(self) -> Generation | None:
        return self.active or self.pending


class RoleSecret(_Record):
    name: RoleName
    password: str = Field(min_length=32, max_length=256)
    verifier: Verifier


class SecretRoles(_Record):
    gateway: RoleSecret
    runner: RoleSecret

    def of(self, cls: GenerationClass) -> RoleSecret:
        return self.gateway if cls == "gateway" else self.runner


class GenerationSecret(_Record):
    """``generations/<n>.json``: the only place a login password exists."""

    number: int = Field(ge=0)
    home: str
    roles: SecretRoles


@dataclass(frozen=True)
class VerifiedGeneration:
    """Receipt: the catalog holds exactly this generation's logins.

    Constructed only by ``roles.verify_generation`` after an exact comparison of
    attributes, stored verifier, membership options and shared dependencies.
    """

    number: int
    credential_digest: str
    roles: tuple[str, str]


@dataclass(frozen=True)
class SurvivingSession:
    pid: int
    role: str | None
    database: str | None
    state: str | None
    xact_start: str | None
    wait_event: str | None


@dataclass(frozen=True)
class PreparedTransaction:
    gid: str
    owner: str | None
    database: str | None


@dataclass(frozen=True)
class ClosureEvidence:
    """Receipt: a census after termination found no session of ``roles``,
    no orphaned session of a dropped role, and no prepared transaction.

    Constructed only by ``fence.prove_closure``; a sent signal never produces it.
    """

    roles: tuple[str, ...]
    terminated: int
    rounds: int


class ClosureRefusedError(AuthorityRefusedError):
    """Closure could not be proven; the evidence names what survived."""

    def __init__(
        self,
        reason: str,
        *,
        survivors: tuple[SurvivingSession, ...] = (),
        prepared: tuple[PreparedTransaction, ...] = (),
        unconfirmed_signals: tuple[int, ...] = (),
    ) -> None:
        self.survivors = survivors
        self.prepared = prepared
        self.unconfirmed_signals = unconfirmed_signals
        super().__init__(
            f"closure not proven: {reason}; survivors={list(survivors)} "
            f"prepared={list(prepared)} unconfirmed_signals={list(unconfirmed_signals)}"
        )
