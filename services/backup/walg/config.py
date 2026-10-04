"""The WAL-G configuration file: validated, never translated, never echoed.

`AVA_WALG_CONFIG_FILE` names a 0600 JSON file in WAL-G's own format. WAL-G reads
it itself (`wal-g --config <file> ...`), so Ava only checks that it is usable
before Postgres is pointed at it, and pins the encryption key's fingerprint.

The libsodium key is the single point of loss: without it every backup and every
archived WAL segment is unreadable, and a regenerated key file silently mixes two
keys in one chain. The first load pins the key's fingerprint under
`$AVA_HOME/backups/walg/key-id` (trust on first use); a later key file that does
not match is refused and alerted. Nothing here rotates a key: a new key means a
new bucket prefix. No secret value (access key, key material) is ever returned,
logged or put in an error: messages name keys and paths, and the fingerprint is
a truncated SHA-256 that reveals nothing about the key.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from base.config import settings
from base.deploy.release.verified_file import RegularFileReadError, regular_bytes
from base.host.private_storage import create_private_bytes, private_file_problem
from base.paths import ava_home
from services.backup.artifact.names import REMOTE_ROOT

_CONFIG_MAX_BYTES = 64 * 1024
_KEY_MAX_BYTES = 4096
_KEY_HEX = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_CHARS = 16

# WAL-G keys that must be present as non-empty strings. `WALG_LIBSODIUM_KEY_PATH`
# is read separately (it is a path, and the file behind it is checked too).
_REQUIRED_STRINGS = (
    "WALG_OSS_PREFIX",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_ENDPOINT",
    "OSS_REGION",
    "WALG_LIBSODIUM_KEY_PATH",
)

# Bucket prefixes the physical chain must never live under: the logical-dump
# namespace, the scratch area (lifecycle-expired) and the retired physical chain.
_FORBIDDEN_FIRST_SEGMENTS = (REMOTE_ROOT, "ava-pitr-scratch")
_FORBIDDEN_FIRST_SEGMENT_PREFIXES = ("ava-wsl-cutover-",)


class WalgConfigError(RuntimeError):
    """The WAL-G configuration is missing, unusable, or contradicts the pinned key."""


@dataclass(frozen=True)
class WalgConfig:
    """The non-secret facts of a validated configuration."""

    path: Path
    prefix: str
    key_path: Path
    key_fingerprint: str


def configured_path() -> Path | None:
    """The configured file, or None when WAL-G is off (the default)."""
    return settings.walg.walg_config_file


def enabled() -> bool:
    return configured_path() is not None


def key_id_path() -> Path:
    return ava_home() / "backups" / "walg" / "key-id"


def _owner_only_file(path: Path, what: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise WalgConfigError(f"{what} {path} does not exist") from None
    problem = private_file_problem(path)
    if problem is None and mode & 0o077:
        problem = "is not owner-only (mode 0600)"
    if problem is not None:
        raise WalgConfigError(f"{what} {path} {problem}")


def _prefix_problem(prefix: str) -> str | None:
    parts = urlsplit(prefix)
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.scheme != "oss" or not parts.netloc:
        return "WALG_OSS_PREFIX must be oss://<bucket>/<path>"
    if not segments:
        return "WALG_OSS_PREFIX must name a path inside the bucket, not the bucket root"
    first = segments[0]
    if first in _FORBIDDEN_FIRST_SEGMENTS or first.startswith(_FORBIDDEN_FIRST_SEGMENT_PREFIXES):
        return f"WALG_OSS_PREFIX must not live under {first}/"
    return None


def _overwrite_guard_on(raw: object) -> bool:
    """`WALG_PREVENT_WAL_OVERWRITE` as WAL-G reads it: JSON true or the string "true"."""
    return raw is True or (isinstance(raw, str) and raw.strip().lower() == "true")


def read_config(path: Path) -> WalgConfig:
    """Validate the file at `path` and the key file it names; no pin is read or written.

    Raises:
        WalgConfigError: any file is missing, not owner-only, malformed, or lacks
            a required setting. The message never contains a setting's value.
    """
    _owner_only_file(path, "WAL-G config")
    try:
        payload: object = json.loads(regular_bytes(path, max_bytes=_CONFIG_MAX_BYTES))
    except (OSError, RegularFileReadError, ValueError):
        raise WalgConfigError(f"WAL-G config {path} is not readable JSON") from None
    if not isinstance(payload, dict):
        raise WalgConfigError(f"WAL-G config {path} must be a JSON object")
    raw = cast(dict[str, object], payload)

    for name in _REQUIRED_STRINGS:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise WalgConfigError(f"WAL-G config {path} must set {name} to a non-empty string")
    if str(raw["OSS_REGION"]).strip().startswith("oss-"):
        # The endpoint host is `oss-<region>.aliyuncs.com`; the signing region is without the prefix.
        raise WalgConfigError(
            f"WAL-G config {path}: OSS_REGION must be the region id without the oss- prefix "
            "(cn-shanghai, not oss-cn-shanghai); OSS rejects the signature otherwise"
        )
    if raw.get("WALG_LIBSODIUM_KEY_TRANSFORM") != "hex":
        raise WalgConfigError(f"WAL-G config {path} must set WALG_LIBSODIUM_KEY_TRANSFORM to hex")
    if not _overwrite_guard_on(raw.get("WALG_PREVENT_WAL_OVERWRITE")):
        raise WalgConfigError(f"WAL-G config {path} must set WALG_PREVENT_WAL_OVERWRITE to true")
    prefix = str(raw["WALG_OSS_PREFIX"])
    problem = _prefix_problem(prefix)
    if problem is not None:
        raise WalgConfigError(f"WAL-G config {path}: {problem}")

    key_path = Path(str(raw["WALG_LIBSODIUM_KEY_PATH"]))
    return WalgConfig(
        path=path,
        prefix=prefix,
        key_path=key_path,
        key_fingerprint=_key_fingerprint(key_path),
    )


def _key_fingerprint(key_path: Path) -> str:
    _owner_only_file(key_path, "WAL-G encryption key file")
    try:
        text = regular_bytes(key_path, max_bytes=_KEY_MAX_BYTES).decode().strip()
    except (OSError, RegularFileReadError, UnicodeDecodeError):
        raise WalgConfigError(f"WAL-G encryption key file {key_path} is not readable") from None
    if _KEY_HEX.fullmatch(text) is None:
        raise WalgConfigError(
            f"WAL-G encryption key file {key_path} must hold 64 lowercase hex characters "
            "(32 bytes: `openssl rand -hex 32`)"
        )
    return hashlib.sha256(text.encode()).hexdigest()[:_FINGERPRINT_CHARS]


def pinned_key_id() -> str | None:
    """The fingerprint pinned on first use, or None before any load pinned one."""
    path = key_id_path()
    try:
        value = regular_bytes(path, max_bytes=256).decode().strip()
    except FileNotFoundError:
        return None
    except (OSError, RegularFileReadError, UnicodeDecodeError) as exc:
        raise WalgConfigError(f"pinned key id {path} is not readable: {exc}") from None
    if re.fullmatch(rf"[0-9a-f]{{{_FINGERPRINT_CHARS}}}", value) is None:
        raise WalgConfigError(f"pinned key id {path} is malformed")
    return value


def pin_problem(config: WalgConfig) -> str | None:
    """Why `config`'s key is not the pinned one, or None (also None while nothing is pinned)."""
    pinned = pinned_key_id()
    if pinned is None or pinned == config.key_fingerprint:
        return None
    return (
        f"the encryption key at {config.key_path} (fingerprint {config.key_fingerprint}) is not "
        f"the key this home pinned (fingerprint {pinned}); a different key makes the archived "
        "chain unreadable. Restore the pinned key, or start a new bucket prefix with a new key"
    )


def load_walg_config() -> WalgConfig:
    """The validated configuration, pinning the key's fingerprint on first use.

    Called by converge, before Postgres starts: a bad configuration fails
    `ava start` instead of becoming a Postgres whose archive command can never
    succeed.

    Raises:
        WalgConfigError: WAL-G is not configured, the configuration is unusable,
            or its key differs from the pinned one.
    """
    path = configured_path()
    if path is None:
        raise WalgConfigError("AVA_WALG_CONFIG_FILE is not set")
    config = read_config(path)
    problem = pin_problem(config)
    if problem is not None:
        raise WalgConfigError(problem)
    if pinned_key_id() is None:
        try:
            create_private_bytes(key_id_path(), (config.key_fingerprint + "\n").encode())
        except FileExistsError:
            # A concurrent loader pinned first: it must have pinned this very key.
            raced = pin_problem(config)
            if raced is not None:
                raise WalgConfigError(raced) from None
    return config
