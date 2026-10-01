"""A remote agent-runner's database capability: manual delivery, one bundle per unit.

The bootstrap endpoint serves no database credential. A remote agent-runner
unit receives the cluster's runner login only through an explicit operator
step, used for a new runner's join and emergencies; no automated exchange
exists.

- The gateway operator issues a **bundle** (`issue_bundle`): the ACTIVE write
  generation's runner login, the endpoint bootstrap serves, and a binding to one
  unit (machine name + unit home), one generation (number + credential digest),
  a nonce and an expiry. The bundle is sealed with AES-256-GCM under a fresh
  32-byte transport key that is shown to the operator once and never written
  anywhere: the file alone discloses nothing, and any change to it fails
  authentication.
- The unit opens it with that key (`open_bundle`) and installs it
  (`install_bundle`) only when it names this unit, this gateway's served
  endpoint, a generation not older than the installed one, has not expired,
  and the cluster accepts its login. Installation writes
  `$AVA_HOME/db-authority/unit.json` (0600); nothing else on the unit holds the
  login.
- The unit's root launcher delivers that login per service
  (`unit_delivery`), an admitted operator process consumes it
  (`consume_unit`), and the launch digest binds its non-secret reference.
- When the issuing gateway's API is authenticated (a non-empty human secret)
  the capability also carries the unit's API admission (`UnitApi`): the
  generation's runner API token its services present, the digest of the
  gateway token its ops server accepts, and the telemetry relay token. The
  unit never holds the human cluster secret.

A bundle is bound to one unit, but nothing in it is that unit's own: the runner
login and API token are the write generation's, shared by every runner unit, and
the telemetry token is the cluster's. The binding guards against installing on
the wrong unit by mistake, not against theft; what a lost bundle exposes and how
to contain it: `base/cluster/authority/docs/unit-bundle.ava.okf.md`.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from base.cluster.authority.api import API_TOKEN_ENV, telemetry_token, token_digest
from base.cluster.authority.delivery import (
    GENERATION_ENV,
    active_generation,
    require_admitted_runtime,
)
from base.cluster.authority.ledger import (
    _locked,
    _read_private,
    authority_dir,
    read_secret,
)
from base.cluster.authority.model import AuthorityRefusedError, Digest, RoleName
from base.deploy.progress_timeout import UNIT_BUNDLE_MAX_TTL_S
from base.host.net.url_secret import url_with_userinfo
from base.host.private_storage import write_private_bytes

# The operator supplies the transport key through this environment variable
# (from a non-echoing prompt); `ava start` pops it before anything is forwarded.
CAPABILITY_KEY_ENV = "AVA_DB_CAPABILITY_KEY"
_FORMAT = "ava-db-capability/1"
_MAX_BUNDLE_BYTES = 64 * 1024
_KEY_BYTES = 32
_IV_BYTES = 12

MachineName = Annotated[
    str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
]
Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
# repr=False covers every field typed `Secret` in one place (`UnitApi.token`,
# `UnitApi.telemetry`, `UnitCapability.password`).
Secret = Annotated[str, Field(min_length=32, max_length=256, repr=False)]


class UnitCapabilityError(AuthorityRefusedError):
    """A bundle or installed capability this unit may not use."""


def no_capability_message(home: Path) -> str:
    """The refusal a remote agent-runner without an installed capability reports."""
    return (
        f"this agent-runner home ({home}) holds no database capability. On the gateway run "
        f"`ava cluster db-authority issue-unit --machine <this machine> --home {home} --out "
        "<bundle>`, carry the bundle here, export AVA_DB_CAPABILITY_KEY from a non-echoing "
        "prompt with the transport key it printed, and install it with "
        "`ava cluster db-authority install-unit <bundle>` (a first join passes it to "
        "`ava init --db-capability <bundle>`)"
    )


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class UnitIdentity(_Record):
    """One unit: its machine name and its home path on that machine (data)."""

    machine: MachineName
    home: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def absolute_home(self) -> Self:
        home = self.home
        if any(ord(c) < 32 for c in home) or not (
            PurePosixPath(home).is_absolute() or PureWindowsPath(home).is_absolute()
        ):
            raise ValueError("a unit home is an absolute path on the unit's machine")
        return self

    def describe(self) -> str:
        return f"{self.machine}:{self.home}"


class GenerationRef(_Record):
    number: int = Field(ge=0)
    credential_digest: Digest


class UnitApi(_Record):
    """The unit's HTTP admission for one generation (authenticated clusters only)."""

    token: Secret  # the generation's runner API token, presented by the unit
    gateway: Digest  # digest of the gateway API token, accepted by its ops server
    telemetry: Secret  # the OTLP relay ingress bearer (derived from the human secret)


class UnitCapability(_Record):
    """`unit.json`: the runner login of one write generation for one unit, and
    its API admission when the cluster's API is authenticated."""

    version: Literal[1]
    unit: UnitIdentity
    endpoint: str = Field(min_length=1, max_length=2048)
    generation: GenerationRef
    role: RoleName
    password: Secret
    bundle: Hex32
    api: UnitApi | None

    @property
    def dsn(self) -> str:
        return url_with_userinfo(self.endpoint, self.role, self.password)

    @property
    def reference(self) -> dict[str, object]:
        """The only facts about the capability a launch digest carries."""
        return {
            "number": self.generation.number,
            "credential_digest": self.generation.credential_digest,
        }


class Bundle(_Record):
    """The sealed payload: one capability."""

    version: Literal[1]
    purpose: Literal["db-capability"]
    cls: Literal["runner"]
    gateway_home: str
    issued_at: float
    expires_at: float
    capability: UnitCapability

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("a bundle expires after it is issued")
        return self


class _Header(_Record):
    """Readable, authenticated (AEAD associated data), never secret."""

    format: Literal["ava-db-capability/1"]
    machine: MachineName
    home: str
    generation: int = Field(ge=0)
    expires_at: float
    nonce: Hex32


class _Envelope(_Record):
    header: _Header
    iv: str
    ciphertext: str


def _canonical(record: BaseModel) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str, what: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise UnitCapabilityError(f"the bundle's {what} is not valid base64url") from exc


def endpoint_key(url: str) -> tuple[str, int, str]:
    """(host, port, database) of a Postgres URL: the endpoint identity."""
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.port or 5432, parts.path.strip("/")


def credential_free(url: str) -> str:
    """`url` with any password removed; the username stays (names-as-data)."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    return url_with_userinfo(url, unquote(parts.username or ""), "")


def _parse[M: BaseModel](model: type[M], body: bytes, path: Path) -> M:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise UnitCapabilityError(f"{path} is corrupt: {exc}") from exc


# ── gateway: issuance ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssuedBundle:
    """A sealed bundle and the transport key that opens it (shown once)."""

    envelope: bytes
    transport_key: str
    unit: UnitIdentity
    generation: int
    expires_at: float


def issue_bundle(
    home: Path,
    *,
    unit: UnitIdentity,
    endpoint: str,
    cluster_secret: str,
    ttl_s: float,
    now: float | None = None,
) -> IssuedBundle:
    """Seal the ACTIVE generation's runner login (and API admission) for `unit`.

    `home` is the gateway home keeping the ledger; `endpoint` is the
    credential-free database URL bootstrap serves to runners;
    `cluster_secret` is the gateway's human secret, from which only the
    telemetry token is derived (empty = an open API: no `UnitApi`). A home
    without an active generation raises (a pending or revoked one is never
    issued).
    """
    if urlsplit(endpoint).password is not None:
        raise UnitCapabilityError("a capability names the credential-free endpoint")
    if not 0 < ttl_s <= UNIT_BUNDLE_MAX_TTL_S:  # also refuses NaN
        raise UnitCapabilityError(
            "a bundle's lifetime must be positive and at most "
            f"{UNIT_BUNDLE_MAX_TTL_S / 3600:g} hours"
        )
    generation = active_generation(home)
    secret = read_secret(home, generation)
    api = (
        UnitApi(
            token=secret.api.runner,
            gateway=token_digest(secret.api.gateway),
            telemetry=telemetry_token(cluster_secret),
        )
        if cluster_secret
        else None
    )
    issued_at = time.time() if now is None else now
    capability = UnitCapability(
        version=1,
        unit=unit,
        endpoint=endpoint,
        generation=GenerationRef(
            number=generation.number, credential_digest=generation.credential_digest
        ),
        role=secret.roles.runner.name,
        password=secret.roles.runner.password,
        bundle=secrets.token_hex(16),
        api=api,
    )
    bundle = Bundle(
        version=1,
        purpose="db-capability",
        cls="runner",
        gateway_home=str(home),
        issued_at=issued_at,
        expires_at=issued_at + ttl_s,
        capability=capability,
    )
    key = secrets.token_bytes(_KEY_BYTES)
    return IssuedBundle(_seal(bundle, key), _b64(key), unit, generation.number, bundle.expires_at)


def _header(bundle: Bundle) -> _Header:
    return _Header(
        format=_FORMAT,
        machine=bundle.capability.unit.machine,
        home=bundle.capability.unit.home,
        generation=bundle.capability.generation.number,
        expires_at=bundle.expires_at,
        nonce=bundle.capability.bundle,
    )


def _seal(bundle: Bundle, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    header = _header(bundle)
    iv = secrets.token_bytes(_IV_BYTES)
    ciphertext = AESGCM(key).encrypt(iv, _canonical(bundle), _canonical(header))
    envelope = _Envelope(header=header, iv=_b64(iv), ciphertext=_b64(ciphertext))
    return json.dumps(envelope.model_dump(mode="json"), sort_keys=True, indent=2).encode() + b"\n"


def write_bundle(path: Path, envelope: bytes) -> None:
    """Create `path` 0600 exclusively; the parent directory is left as it is."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(envelope)
        stream.flush()
        os.fsync(stream.fileno())


# ── unit: open, install ─────────────────────────────────────────────────────


def open_bundle(data: bytes, transport_key: str, *, now: float | None = None) -> Bundle:
    """Authenticate and decrypt a bundle; refuse anything altered or expired."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(data) > _MAX_BUNDLE_BYTES:
        raise UnitCapabilityError("the file is too large to be a database capability bundle")
    try:
        envelope = _Envelope.model_validate_json(data)
    except ValidationError as exc:
        raise UnitCapabilityError(f"not a database capability bundle: {exc}") from exc
    key = _unb64(transport_key.strip(), "transport key")
    if len(key) != _KEY_BYTES:
        raise UnitCapabilityError("the transport key is not the 32-byte key issue-unit printed")
    try:
        plaintext = AESGCM(key).decrypt(
            _unb64(envelope.iv, "iv"),
            _unb64(envelope.ciphertext, "ciphertext"),
            _canonical(envelope.header),
        )
    except (InvalidTag, ValueError) as exc:
        raise UnitCapabilityError(
            "the bundle does not authenticate under this transport key: it was altered, "
            "or the key belongs to another bundle"
        ) from exc
    bundle = _parse(Bundle, plaintext, Path("<bundle>"))
    if _header(bundle) != envelope.header:
        raise UnitCapabilityError("the bundle's header contradicts its sealed content")
    if (time.time() if now is None else now) >= bundle.expires_at:
        raise UnitCapabilityError("the bundle has expired; issue a new one on the gateway")
    return bundle


def unit_capability_path(home: Path) -> Path:
    return authority_dir(home) / "unit.json"


def load_unit_capability(home: Path) -> UnitCapability | None:
    """The installed capability, bound to `home`; None when none is installed."""
    path = unit_capability_path(home)
    try:
        capability = _parse(UnitCapability, _read_private(path), path)
    except FileNotFoundError:
        return None
    if capability.unit.home != str(home):
        raise UnitCapabilityError(f"{path} belongs to another home")
    return capability


def _require_supersedes(current: UnitCapability | None, new: UnitCapability) -> None:
    if current is None:
        return
    old, fresh = current.generation, new.generation
    if fresh.number < old.number:
        raise UnitCapabilityError(
            f"the bundle carries generation {fresh.number}, older than the installed "
            f"generation {old.number}"
        )
    if fresh.number == old.number and fresh.credential_digest != old.credential_digest:
        raise UnitCapabilityError(
            f"the bundle contradicts the installed generation {old.number}'s credential digest"
        )


def probe_login(dsn: str) -> None:
    """One `SELECT 1` as the capability's login through the served endpoint."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as conn:
        conn.execute("SELECT 1")


def install_bundle(
    home: Path,
    bundle: Bundle,
    *,
    machine: str,
    served_endpoint: str,
    probe: Callable[[str], None] = probe_login,
) -> UnitCapability:
    """Install `bundle` as this unit's capability after every binding check.

    The bundle must name this unit (machine and home), the endpoint the gateway
    serves right now, a generation not older than the installed one, and a
    login the cluster accepts: a revoked generation's login is refused by the
    database, so its bundle never installs.
    """
    capability = bundle.capability
    unit = UnitIdentity(machine=machine, home=str(home))
    if capability.unit != unit:
        raise UnitCapabilityError(
            f"the bundle was issued for {capability.unit.describe()}, not {unit.describe()}"
        )
    if endpoint_key(capability.endpoint) != endpoint_key(served_endpoint):
        raise UnitCapabilityError(
            "the bundle names another database endpoint than the gateway serves; issue a new bundle"
        )
    _require_supersedes(load_unit_capability(home), capability)
    try:
        probe(capability.dsn)
    except Exception as exc:
        raise UnitCapabilityError(
            f"the cluster refuses generation {capability.generation.number}'s runner login "
            f"(a revoked generation, or an unreachable endpoint): {exc}"
        ) from exc
    with _locked(home):
        write_private_bytes(unit_capability_path(home), _canonical(capability) + b"\n")
    return capability


# ── unit: delivery ──────────────────────────────────────────────────────────


def require_unit_capability(home: Path) -> UnitCapability:
    capability = load_unit_capability(home)
    if capability is None:
        raise UnitCapabilityError(no_capability_message(home))
    return capability


def unit_delivery(home: Path) -> dict[str, str]:
    """The launch-environment delivery of the installed runner login."""
    capability = require_unit_capability(home)
    return {"AVA_DB_URL": capability.dsn, GENERATION_ENV: str(capability.generation.number)}


def unit_api_delivery(home: Path) -> dict[str, str]:
    """The launch-environment API token of the installed capability; none when
    the cluster's API is open."""
    api = require_unit_capability(home).api
    return {} if api is None else {API_TOKEN_ENV: api.token}


def unit_reference(home: Path) -> dict[str, object] | None:
    capability = load_unit_capability(home)
    return None if capability is None else capability.reference


def is_delivered_login(home: Path, url: str | None, generation: str | None) -> bool:
    """Whether (`url`, `generation`) is exactly this home's installed delivery."""
    try:
        capability = load_unit_capability(home)
    except AuthorityRefusedError:
        return False
    return (
        capability is not None
        and url == capability.dsn
        and generation == str(capability.generation.number)
    )


def consume_unit(home: Path) -> UnitCapability:
    """The installed runner login for a process the launcher did not inject,
    only while it runs this home's admitted runtime."""
    code_root = Path(__file__).resolve().parents[3]
    require_admitted_runtime(home, code_root=code_root)
    return require_unit_capability(home)


def telemetry_bearer(home: Path, cluster_secret: str) -> str | None:
    """The OTLP relay ingress bearer `home` presents and accepts, or None (open).

    A remote unit (an installed capability) uses the telemetry token its
    capability carries; it holds no human secret. The gateway derives the same
    token from its human secret. None means the cluster's API is open: no
    authenticated ingress exists.
    """
    capability = load_unit_capability(home)
    if capability is not None:
        return None if capability.api is None else capability.api.telemetry
    return telemetry_token(cluster_secret) if cluster_secret else None
