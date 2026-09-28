"""Release coordinator channel authentication, keyed by the unit enrollment secret.

During a fleet release the coordinator's listener serves unit executors while
the gateway application, its bearer and the database may all be down, so the
channel authenticates with the one durable per-unit secret both sides hold:
the enrollment (`shared.cluster.authority.unit`). The human bearer and write
generations never authenticate this channel.

- **Requests** carry a `RequestProof`: an HMAC-SHA256 over the protocol tag,
  the operation id, the unit, the method, the path, the SHA-256 of the body, a
  timestamp and a nonce. The listener checks the proof against the gateway's
  CURRENT record for the unit (`load_enrollment`), so a rotated or revoked
  enrollment stops authenticating at once, and a `ReplayWindow` per operation
  refuses a nonce it has already admitted and any timestamp outside the skew.
- **Sealed responses** (a capability handed to one unit) are AES-256-GCM under
  a key derived from (enrollment secret, operation, unit): another unit, or the
  same unit in another operation, cannot open them.

Keys are derived with HKDF-SHA256 from the secret with distinct labels, so the
request key never equals a sealing key. Secret values never appear in errors.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from shared.cluster.authority.model import AuthorityRefusedError
from shared.cluster.authority.unit import Enrollment

PROTOCOL = "ava-coordinator/1"
MAX_SKEW_S = 300
_IV_BYTES = 12
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ChannelRefusedError(AuthorityRefusedError):
    """A coordinator-channel request or sealed payload that does not authenticate."""


@dataclass(frozen=True)
class RequestProof:
    """What a unit sends beside one request; carries no secret."""

    enrollment_id: str
    timestamp: int
    nonce: str
    signature: str

    def __post_init__(self) -> None:
        if not _HEX32.match(self.enrollment_id) or not _HEX32.match(self.nonce):
            raise ChannelRefusedError("a request proof names a malformed enrollment or nonce")
        if not _HEX64.match(self.signature):
            raise ChannelRefusedError("a request proof carries a malformed signature")


def _derive(enrollment: Enrollment, label: str, *context: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = "\0".join((PROTOCOL, label, *context)).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        enrollment.secret.encode()
    )


def _message(
    enrollment: Enrollment,
    *,
    operation: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> bytes:
    parts = (
        PROTOCOL,
        operation,
        enrollment.unit.key,
        enrollment.enrollment_id,
        method.upper(),
        path,
        hashlib.sha256(body).hexdigest(),
        str(timestamp),
        nonce,
    )
    if any("\n" in part for part in parts):
        raise ChannelRefusedError("a coordinator request field contains a line break")
    return "\n".join(parts).encode()


def _mac(enrollment: Enrollment, message: bytes) -> str:
    return hmac.new(_derive(enrollment, "request"), message, hashlib.sha256).hexdigest()


def sign_request(
    enrollment: Enrollment,
    *,
    operation: str,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> RequestProof:
    """The proof a unit executor attaches to one coordinator request."""
    timestamp = int(time.time() if now is None else now)
    nonce = secrets.token_hex(16)
    message = _message(
        enrollment,
        operation=operation,
        method=method,
        path=path,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
    )
    return RequestProof(enrollment.enrollment_id, timestamp, nonce, _mac(enrollment, message))


@dataclass
class ReplayWindow:
    """Nonces admitted for one operation within the timestamp skew.

    Held in memory by the coordinator listener, which lives exactly as long as
    its operation. A continuation starts a new window, so a request captured
    less than the skew before a coordinator restart can arrive once more:
    every channel request must be idempotent (a report names the instruction
    it answers). The listener's handler threads share one window, so `admit`
    prunes, checks and records under one lock: a nonce is admitted at most
    once, and concurrent prunes never trip over each other.
    """

    operation: str
    skew_s: int = MAX_SKEW_S
    _seen: dict[tuple[str, str], int] = field(
        default_factory=dict[tuple[str, str], int], init=False, repr=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def admit(self, unit_key: str, proof: RequestProof, now: float) -> None:
        if abs(now - proof.timestamp) > self.skew_s:
            raise ChannelRefusedError("the coordinator request is outside the allowed clock skew")
        entry = (unit_key, proof.nonce)
        with self._lock:
            for key, stamp in list(self._seen.items()):
                if abs(now - stamp) > self.skew_s:
                    del self._seen[key]
            if entry in self._seen:
                raise ChannelRefusedError("the coordinator request replays an admitted nonce")
            self._seen[entry] = proof.timestamp


def verify_request(
    enrollment: Enrollment,
    proof: RequestProof,
    *,
    window: ReplayWindow,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> None:
    """Admit one request against the gateway's current record for its unit.

    `enrollment` is the gateway record (never the unit's copy); the operation is
    the window's. The nonce is recorded only after the signature verified, so a
    forged request cannot burn a legitimate nonce.
    """
    if not hmac.compare_digest(proof.enrollment_id, enrollment.enrollment_id):
        raise ChannelRefusedError(
            "the coordinator request names another enrollment (rotated or revoked)"
        )
    message = _message(
        enrollment,
        operation=window.operation,
        method=method,
        path=path,
        body=body,
        timestamp=proof.timestamp,
        nonce=proof.nonce,
    )
    if not hmac.compare_digest(_mac(enrollment, message), proof.signature):
        raise ChannelRefusedError("the coordinator request does not authenticate")
    window.admit(enrollment.unit.key, proof, time.time() if now is None else now)


def _seal_context(enrollment: Enrollment, operation: str) -> tuple[bytes, bytes]:
    key = _derive(enrollment, "seal", operation, enrollment.unit.key)
    associated = "\0".join((PROTOCOL, operation, enrollment.unit.key)).encode()
    return key, associated


def seal(enrollment: Enrollment, *, operation: str, plaintext: bytes) -> bytes:
    """Seal `plaintext` for exactly this unit in exactly this operation."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, associated = _seal_context(enrollment, operation)
    iv = secrets.token_bytes(_IV_BYTES)
    return iv + AESGCM(key).encrypt(iv, plaintext, associated)


def open_sealed(enrollment: Enrollment, *, operation: str, sealed: bytes) -> bytes:
    """Open a payload `seal` produced for this unit and operation; refuse anything else."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, associated = _seal_context(enrollment, operation)
    if len(sealed) <= _IV_BYTES:
        raise ChannelRefusedError("the sealed coordinator payload is truncated")
    try:
        return AESGCM(key).decrypt(sealed[:_IV_BYTES], sealed[_IV_BYTES:], associated)
    except InvalidTag as exc:
        raise ChannelRefusedError(
            "the sealed coordinator payload does not open for this unit and operation"
        ) from exc
