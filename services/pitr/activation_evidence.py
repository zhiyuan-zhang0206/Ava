"""Digest tolerance across the store-abstraction renames.

Activation records store their manifest payloads alongside a digest of
those exact bytes. The store abstraction renames manifest fields, so a
record written before it stores legacy-shaped JSON whose digest no
longer equals the canonical re-serialization — both byte forms must be
accepted while any third value fails.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import cast


def stored_digest_matches(*, raw: str, canonical: str, expected: str) -> bool:
    """True when ``expected`` is the digest of the raw stored JSON bytes or
    of today's canonical re-serialization."""
    return (
        hashlib.sha256(raw.encode()).hexdigest() == expected
        or hashlib.sha256(canonical.encode()).hexdigest() == expected
    )


def _embedded_candidate_raw(protected_manifest: str) -> str:
    """The candidate JSON exactly as serialized inside a stored protected
    manifest (both shapes use canonical key-sorted serialization)."""
    raw: object = json.loads(protected_manifest)
    if not isinstance(raw, dict):
        raise TypeError("protected manifest lacks an embedded candidate")
    payload = cast(dict[str, object], raw)
    if not isinstance(payload.get("candidate"), dict):
        raise TypeError("protected manifest lacks an embedded candidate")
    return json.dumps(payload["candidate"], sort_keys=True, separators=(",", ":"))


def validate_wal_remote_evidence(
    *,
    ack: dict[str, str],
    viewer: dict[str, str],
    exact: dict[str, str] | None,
    verification_deadline: str | None,
    credential_evidence: dict[str, str] | None,
) -> None:
    """Cross-check the durable WAL ACK against the viewer-side proof and the
    activation's exact WAL intent; every deviation raises."""
    immutable = (
        "timeline",
        "segment",
        "bucket_name",
        "object_prefix",
        "object_name",
        "generation",
        "ciphertext_size",
        "ciphertext_crc32c",
        "key_id",
        "encryption_format",
        "source_sha256",
        "source_size",
    )
    if any(ack[name] != viewer[name] for name in immutable):
        raise ValueError("PITR ACK and viewer immutable evidence differ")
    frozen = credential_evidence or {}
    if exact is None or any(
        (
            ack["segment"] != exact["segment"],
            ack["timeline"] != exact["timeline"],
            ack["bucket_name"] != frozen.get("store_target"),
            ack["object_prefix"] != frozen.get("object_prefix"),
            ack["key_id"] != frozen.get("backup_key_id"),
            viewer["viewer_id"] != frozen.get("viewer_identity"),
        )
    ):
        raise ValueError("PITR remote evidence differs from WAL intent")
    expected_object = (
        f"{frozen.get('object_prefix', '').rstrip('/')}/wal/"
        f"{ack['segment'][:8]}/{ack['segment']}.enc"
    )
    if ack["object_name"] != expected_object:
        raise ValueError("PITR remote object name is not canonical")
    if int(ack["generation"]) <= 0 or int(ack["ciphertext_size"]) <= 0:
        raise ValueError("PITR remote identity must have positive generation and size")
    if int(ack["source_size"]) <= 0 or not re.fullmatch(r"[0-9a-f]{64}", ack["source_sha256"]):
        raise ValueError("PITR remote evidence has invalid source identity")
    if not re.fullmatch(r"[A-Za-z0-9+/]{6}==", ack["ciphertext_crc32c"]):
        raise ValueError("PITR remote evidence has invalid CRC32C")
    intent_at = datetime.fromisoformat(exact["switch_intent_at"])
    acknowledged = datetime.fromisoformat(ack["acknowledged_at"])
    observed = datetime.fromisoformat(viewer["observed_at"])
    if verification_deadline is None:
        raise ValueError("PITR remote proof lacks its durable deadline")
    deadline = datetime.fromisoformat(verification_deadline)
    if not intent_at <= acknowledged <= observed <= deadline:
        raise ValueError("PITR remote evidence falls outside its durable deadline")
