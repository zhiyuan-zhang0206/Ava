"""Typed records, the receipt and refusals for the home's write generation.

Everything here is data: the ledger and secret-file shapes, the receipt only
the catalog code constructs, and the refusals. Names are recorded data and are
never parsed back into authority; a role name prefix grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GenerationClass = Literal["gateway", "runner"]
CLASSES: tuple[GenerationClass, ...] = ("gateway", "runner")

# Stable NOLOGIN capability groups. `ava_runner` is the historical runner login,
# demoted in place so its existing grants keep working.
GATEWAY_GROUP = "ava_gateway"
RUNNER_GROUP = "ava_runner"

RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
# Credential digest — a one-way sha256 over the secret file's bytes, never
# reversible into a login — is deliberately excluded from this hiding: it is
# the safe-to-log fingerprint the whole authority library uses in launch
# digests and journals (see `base/cluster/authority/ledger.py:credential_digest`).
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# repr=False: a SCRAM stored verifier is derived from the role's password and
# is not meant to be displayed — hiding it here covers every field typed
# `Verifier` in one place (`RoleSecret.verifier`, `PoolerAdmin.verifier`).
Verifier = Annotated[
    str, StringConstraints(pattern=r"^SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$"), Field(repr=False)
]


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


# The gateway and runner login names of the home's one generation.
GENERATION_NAMES: tuple[str, str] = ("ava_g0_gateway", "ava_g0_runner")


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
    # `cutover`: the generation of a home converted before births minted the
    # ledger. Nothing mints it now; recorded ledgers keep it.
    kind: Literal["birth", "cutover"]
    # Fields of a retired shape, still present as nulls in a ledger written
    # before write generations stopped rotating: read, never written.
    operation: None = Field(default=None, exclude=True)
    direction: None = Field(default=None, exclude=True)


class Generation(_Record):
    # The only generation a home ever has. The number stays because the role
    # names (`ava_g0_*`), `generations/0.json` and every delivered environment
    # carry it; renaming them would itself be a credential change.
    number: Literal[0]
    gateway: RoleName
    runner: RoleName
    credential_digest: Digest
    origin: Origin

    @property
    def roles(self) -> tuple[str, str]:
        return self.gateway, self.runner

    def role(self, cls: GenerationClass) -> str:
        return self.gateway if cls == "gateway" else self.runner


class Ledger(_Record):
    """The home's write-generation authority record.

    Born with no generation; birth records one as ``pending`` and admission
    moves it to ``active`` once the pooler serves it and both logins answer.
    A home has exactly this one generation: nothing revokes or replaces it.
    """

    version: Literal[1]
    home: str
    owner: RoleName
    groups: Groups
    active: Generation | None = None
    pending: Generation | None = None
    # Fields of a retired shape: a ledger written before write generations
    # stopped rotating carries `counter: 0` and `revoked: []`. They are read so
    # that ledger loads, refuse anything that records a rotation, and are never
    # written back.
    counter: Literal[0] | None = Field(default=None, exclude=True)
    revoked: tuple[()] = Field(default=(), exclude=True)

    @model_validator(mode="after")
    def one_generation(self) -> Self:
        if self.active is not None and self.pending is not None:
            raise ValueError("a pending generation exists only while none is active")
        if self.counter is not None and self.generation is None:
            raise ValueError("a recorded counter names a generation the ledger does not hold")
        return self

    @model_validator(mode="after")
    def names_distinct(self) -> Self:
        reserved = {self.owner, self.groups.gateway, self.groups.runner}
        if len(reserved) != 3:
            raise ValueError("the schema owner is not a capability group")
        if self.generation is not None and (
            len(set(self.generation.roles)) != 2 or reserved & set(self.generation.roles)
        ):
            raise ValueError("generation logins must be unique and distinct from owner and groups")
        return self

    @property
    def generation(self) -> Generation | None:
        """The home's one generation: the active one, else the pending one."""
        return self.active or self.pending


class RoleSecret(_Record):
    name: RoleName
    password: str = Field(min_length=32, max_length=256, repr=False)
    verifier: Verifier


class SecretRoles(_Record):
    gateway: RoleSecret
    runner: RoleSecret

    def of(self, cls: GenerationClass) -> RoleSecret:
        return self.gateway if cls == "gateway" else self.runner


class ApiTokens(_Record):
    """One write generation's machine API tokens, one per class.

    The gateway accepts the ACTIVE generation's tokens as HTTP bearers (a
    revoked generation's never); a runner's ops server accepts the gateway
    token. Machine callers present their class token from the launch
    environment, so a stale caller loses the API with its database login.
    """

    gateway: str = Field(min_length=32, max_length=256, repr=False)
    runner: str = Field(min_length=32, max_length=256, repr=False)

    def of(self, cls: GenerationClass) -> str:
        return self.gateway if cls == "gateway" else self.runner


class GenerationSecret(_Record):
    """``generations/<n>.json``: the only place a login password or a machine
    API token exists."""

    number: Literal[0]
    home: str
    roles: SecretRoles
    api: ApiTokens


@dataclass(frozen=True)
class VerifiedGeneration:
    """Receipt: the catalog holds exactly this generation's logins.

    Constructed only by ``roles.verify_generation`` after an exact comparison of
    attributes, stored verifier, membership options and shared dependencies.
    """

    number: int
    credential_digest: str
    roles: tuple[str, str]
