"""Machine API admission from the write generation.

Machine callers present the generation's class token instead of the human
cluster secret, so a machine holds no human bearer:

- The generation's secret file carries one API token per class
  (``GenerationSecret.api``). The gateway accepts the ACTIVE generation's
  tokens (``acceptance``), never a pending one. A runner's ops server accepts
  the gateway-class token only.
- The root launcher delivers a service its class token in ``AVA_API_TOKEN``,
  exactly like its database login, and only while the API is authenticated
  (a non-empty human secret on the gateway, an API-bearing unit capability on
  a remote unit). An empty-secret single box keeps its open API.
- The OTLP relay ingress is telemetry, not a write path. Its bearer is derived
  from the human secret (``telemetry_token``), so remote units receive it in
  their capability without ever holding the secret itself; it changes only
  when the human secret rotates.

Settings-free: the launcher, the boot pass and the gateway read the same
private store. Comparisons are constant-time over SHA-256 digests, so the
accepting side never needs a token in clear. A client reads its own token
with `base.cluster.auth.delivered_token` (stdlib-only, safe during boot) and presents
it through `base.cluster.machine.gateway_bearer`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from pathlib import Path

from base.cluster.auth import API_TOKEN_ENV as API_TOKEN_ENV
from base.cluster.authority.delivery import active_generation
from base.cluster.authority.ledger import authority_dir, ledger_path, load_ledger, read_secret
from base.cluster.authority.model import CLASSES, GenerationClass
from base.host.private_storage import write_private_bytes

_SCHEME = "Bearer "
_TELEMETRY_LABEL = b"ava-telemetry-ingress/1"

# home -> (ledger file identity, accepted digests); activation rewrites the
# ledger atomically, so its identity changes and the next request reloads.
_acceptance_cache: dict[Path, tuple[tuple[int, int, int], dict[GenerationClass, str]]] = {}


def token_digest(token: str) -> str:
    """SHA-256 of a bearer token: the form an accepting side keeps and compares."""
    return hashlib.sha256(token.encode()).hexdigest()


def telemetry_token(cluster_secret: str) -> str:
    """The OTLP relay ingress bearer of a cluster whose human secret is `cluster_secret`.

    One-way (HMAC-SHA256 under the secret): a unit holding it learns nothing
    about the secret. An empty secret has no authenticated ingress.
    """
    if not cluster_secret:
        raise ValueError("an open cluster (empty secret) has no telemetry ingress token")
    return hmac.new(cluster_secret.encode(), _TELEMETRY_LABEL, hashlib.sha256).hexdigest()


TELEMETRY_TOKEN_FILE = "telemetry-token"  # noqa: S105 — a file name, not a credential


def telemetry_token_path(home: Path) -> Path:
    """The private file the gateway home's root keeps the telemetry token in."""
    return authority_dir(home) / TELEMETRY_TOKEN_FILE


def publish_telemetry_token(home: Path, cluster_secret: str) -> None:
    """Derive the telemetry token from the human secret and keep it in the home's private file
    (0600), or remove the file while the cluster's API is open (an empty secret).

    The one place the secret becomes the token: the root that holds the secret (the gateway home's
    start) publishes it here, and the gateway-side services that need the token (the heartbeat's
    observability-station probe) read the file with `read_telemetry_token` instead of holding
    the secret. A rotated secret reaches them at the next start, which republishes.
    """
    path = telemetry_token_path(home)
    if not cluster_secret:
        path.unlink(missing_ok=True)
        return
    write_private_bytes(path, telemetry_token(cluster_secret).encode())


def read_telemetry_token(home: Path) -> str | None:
    """The published telemetry token, or None when none was published (an open cluster, or a
    start that has not run yet)."""
    try:
        body = telemetry_token_path(home).read_bytes()
    except FileNotFoundError:
        return None
    return body.decode().strip() or None


def bearer_class(
    authorization: str | None, accepted: Mapping[GenerationClass, str]
) -> GenerationClass | None:
    """The class whose token `authorization` presents as `Bearer <token>`, else None.

    Every accepted digest is compared in constant time, whatever matched.
    """
    if not accepted or not authorization or not authorization.startswith(_SCHEME):
        return None
    presented = token_digest(authorization[len(_SCHEME) :])
    matched: GenerationClass | None = None
    for cls, digest in accepted.items():
        if hmac.compare_digest(presented, digest):
            matched = cls
    return matched


def api_token(home: Path, cls: GenerationClass) -> str:
    """The active generation's `cls` API token (a pending one never)."""
    return read_secret(home, active_generation(home)).api.of(cls)


def acceptance(home: Path) -> dict[GenerationClass, str]:
    """Digests of the tokens the gateway at `home` accepts, by class.

    The ACTIVE generation's two tokens; none without a ledger (a remote-managed
    plane has no generations) or while no generation is active (a birth not yet
    admitted). A ledger or secret the store refuses raises.
    """
    path = ledger_path(home)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    identity = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _acceptance_cache.get(home)
    if cached is not None and cached[0] == identity:
        return cached[1]
    ledger = load_ledger(home)
    accepted: dict[GenerationClass, str] = {}
    if ledger is not None and ledger.active is not None:
        tokens = read_secret(home, ledger.active).api
        accepted = {cls: token_digest(tokens.of(cls)) for cls in CLASSES}
    _acceptance_cache[home] = (identity, accepted)
    return accepted
