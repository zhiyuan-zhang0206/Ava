"""Validated private-ledger schema for the external operator skill bridge."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from cli.commands._external_agent_skill_fs import (
    _ClientConflictError,
    _exists,
    _manifest_digest,
    _read_regular,
)
from shared.private_storage import write_private_bytes

_FORMAT = 5
_MARKER_NAME = ".ava-managed.json"
_SKILL_NAME = "operating-ava-cluster"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")


def _ownership_marker(installation_id: str, generation_id: str, source_digest: str) -> bytes:
    return (
        json.dumps(
            {
                "format": _FORMAT,
                "generation_id": generation_id,
                "installation_id": installation_id,
                "owner": "ava",
                "skill": _SKILL_NAME,
                "source_digest": source_digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _stage_manifest(source_manifest: list[dict[str, Any]], marker: bytes) -> list[dict[str, Any]]:
    return sorted(
        [
            *source_manifest,
            {
                "kind": "file",
                "mode": 0o600,
                "path": _MARKER_NAME,
                "sha256": hashlib.sha256(marker).hexdigest(),
            },
        ],
        key=lambda item: str(item["path"]),
    )


def _parse_record(path: Path) -> dict[str, Any] | None:
    if not _exists(path):
        return None
    data, _ = _read_regular(path, source=False)
    try:
        value: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _ClientConflictError("Ava ownership ledger is invalid") from exc
    if not isinstance(value, dict):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    return cast(dict[str, Any], value)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _valid_manifest(value: object) -> bool:
    if not isinstance(value, list):
        return False
    paths: set[str] = set()
    for item_value in cast(list[object], value):
        if not isinstance(item_value, dict):
            return False
        item = cast(dict[str, Any], item_value)
        kind = item.get("kind")
        if kind == "file":
            required = {"kind", "mode", "path", "sha256"}
        elif kind == "directory":
            required = {"kind", "mode", "path"}
        else:
            return False
        path = item.get("path")
        mode = item.get("mode")
        if (
            set(item) != required
            or not isinstance(path, str)
            or path in paths
            or (path != "." and (path.startswith("/") or ".." in Path(path).parts))
            or not isinstance(mode, int)
            or not 0 <= mode <= 0o7777
            or (kind == "file" and not _valid_digest(item.get("sha256")))
        ):
            return False
        paths.add(path)
    for item_value in cast(list[object], value):
        if isinstance(item_value, dict):
            item = cast(dict[str, object], item_value)
            if item.get("path") == "." and item.get("kind") == "directory":
                return True
    return False


def _load_ledger(path: Path, client_key: str) -> dict[str, Any] | None:
    record = _parse_record(path)
    if record is None:
        return None
    required = {
        "client",
        "format",
        "installation_id",
        "installed",
        "transaction",
        "garbage",
    }
    if set(record) != required or record["client"] != client_key or record["format"] != _FORMAT:
        raise _ClientConflictError("Ava ownership ledger is invalid")
    if not _valid_id(record["installation_id"]):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    installed_value: object = record["installed"]
    if installed_value is not None:
        if not isinstance(installed_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        installed = cast(dict[str, Any], installed_value)
        if (
            set(installed) != {"digest", "generation_id", "manifest", "source_digest"}
            or not all(_valid_digest(installed[key]) for key in ("digest", "source_digest"))
            or not _valid_id(installed["generation_id"])
            or not _valid_manifest(installed["manifest"])
            or _manifest_digest(installed["manifest"]) != installed["digest"]
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    transaction_value: object = record["transaction"]
    if transaction_value is not None:
        if not isinstance(transaction_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        transaction = cast(dict[str, Any], transaction_value)
        if (
            set(transaction)
            != {
                "claim_state",
                "expected_digest",
                "expected_manifest",
                "generation_id",
                "source_digest",
                "stage_state",
            }
            or not _valid_id(transaction["generation_id"])
            or not _valid_digest(transaction["source_digest"])
            or not _valid_digest(transaction["expected_digest"])
            or not _valid_manifest(transaction["expected_manifest"])
            or _manifest_digest(transaction["expected_manifest"]) != transaction["expected_digest"]
            or transaction["claim_state"] not in {"idle", "claiming", "claimed"}
            or transaction["stage_state"] not in {"preparing", "publishing", "published"}
            or (transaction["claim_state"] != "idle" and transaction["stage_state"] != "published")
            or (transaction["claim_state"] != "idle" and installed_value is None)
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    garbage_value: object = record["garbage"]
    if not isinstance(garbage_value, list):
        raise _ClientConflictError("Ava ownership ledger is invalid")
    for item_value in cast(list[object], garbage_value):
        if not isinstance(item_value, dict):
            raise _ClientConflictError("Ava ownership ledger is invalid")
        item = cast(dict[str, Any], item_value)
        if (
            set(item) != {"kind", "location", "manifest", "path_generation_id"}
            or item["kind"] not in {"stage", "previous"}
            or item["location"] not in {"source", "claiming", "quarantine"}
            or not _valid_id(item["path_generation_id"])
            or not _valid_manifest(item["manifest"])
        ):
            raise _ClientConflictError("Ava ownership ledger is invalid")
    return record


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_private_bytes(path, (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode())
