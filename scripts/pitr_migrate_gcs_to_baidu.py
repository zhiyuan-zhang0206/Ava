# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
"""One-shot migration of the PITR object store from GCS to Baidu Netdisk.

Reads every blob under the GCS prefix, streams it into the Baidu
three-phase engine (sidecar included), verifies both ends agree on size
and md5, records the identity mapping, and rewrites the local identity
records (ACKs, candidate manifests, protected manifests) from GCS
generations to Baidu fs_id:md5 pins. The GCS bucket is never mutated.

Run with the PITR daemons stopped and no in-flight activation (see
conventions/pitr-backend-switchover.md for the full procedure):

  python scripts/pitr_migrate_gcs_to_baidu.py \
      --gcs-project P --gcs-bucket B --gcs-prefix ava-pitr \
      --gcs-credentials viewer.json \
      --baidu-app-root /apps/ava/ava-pitr \
      --baidu-credentials baidu.json --baidu-token baidu-token.json \
      --records-root ~/.ava/physical-backup

Rollback: restore the snapshot tarball (written next to the records),
point AVA_PITR_STORE_BACKEND back at gcs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import google_crc32c
from google.cloud import storage
from google.oauth2 import service_account

from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.baidu_token import BaiduCredentials, BaiduTokenManager
from services.pitr.base_manifest import CandidateManifest
from services.pitr.checksums import MD5
from services.pitr.restore_manifest import ProtectedManifest, candidate_sha256
from services.pitr.uploader import ack_manifest_from_raw

_RECORD_DIRS = ("base-manifests", "protected-manifests", "protected-pending", "ack")
_TERMINAL_PHASES = {"protected", "rolled_back"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcs-project", required=True)
    parser.add_argument("--gcs-bucket", required=True)
    parser.add_argument("--gcs-prefix", required=True)
    parser.add_argument("--gcs-credentials", required=True, type=Path)
    parser.add_argument("--baidu-app-root", required=True)
    parser.add_argument("--baidu-credentials", required=True, type=Path)
    parser.add_argument("--baidu-token", required=True, type=Path)
    parser.add_argument("--records-root", required=True, type=Path)
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="snapshot tarball path (default: <records-root>/migration-snapshot-<ts>.tar.gz)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        help="mapping file path (default: <records-root>/migration-mapping.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="enumerate + verify, write nothing")
    return parser.parse_args()


def _client(args: argparse.Namespace) -> tuple[storage.Client, BaiduObjectStore]:
    credentials = service_account.Credentials.from_service_account_file(str(args.gcs_credentials))
    gcs = storage.Client(project=args.gcs_project, credentials=credentials)
    baidu = BaiduObjectStore(
        app_root=args.baidu_app_root,
        token_manager=BaiduTokenManager(BaiduCredentials(args.baidu_credentials), args.baidu_token),
        timeout_seconds=600,
    )
    return gcs, baidu


def _preflight(records_root: Path) -> None:
    operation = records_root / "operation.json"
    if not operation.is_file():
        return
    raw: object = json.loads(operation.read_text())
    if not isinstance(raw, dict):
        raise SystemExit("operation.json is not an object — resolve before migrating")
    phase = str(raw.get("phase") or "")
    if phase not in _TERMINAL_PHASES:
        raise SystemExit(
            f"in-flight activation (phase={phase!r}) — the migration needs a quiet window"
        )


def _snapshot(records_root: Path, snapshot_path: Path) -> None:
    if snapshot_path.exists():
        raise SystemExit(f"snapshot already exists: {snapshot_path}")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(snapshot_path, "w:gz") as archive:
        for name in [*sorted(_RECORD_DIRS), "operation.json"]:
            path = records_root / name
            if path.exists():
                archive.add(path, arcname=name)
    print(f"snapshot written: {snapshot_path}")


def _blobs(gcs: storage.Client, args: argparse.Namespace) -> list[Any]:
    prefix = args.gcs_prefix.rstrip("/") + "/"
    blobs = [
        blob
        for blob in cast(list[Any], gcs.bucket(args.gcs_bucket).list_blobs(prefix=prefix))
        if not str(blob.name).endswith("/")
    ]
    if any(blob.generation is None or blob.size is None for blob in blobs):
        raise SystemExit("GCS listing returned a blob without generation or size")
    blobs.sort(key=lambda blob: str(blob.name))
    return blobs


def _migrate_object(
    *,
    baidu: BaiduObjectStore,
    blob: Any,
    object_name: str,
    size: int,
    metadata: dict[str, str],
) -> dict[str, str]:
    """Download one GCS blob, verify its crc32c, stream it into the Baidu
    engine, and return the mapping row (md5 verified on both ends)."""
    with tempfile.TemporaryDirectory(prefix="pitr-migrate-") as scratch:
        staging = Path(scratch) / "object.enc"
        blob.download_to_filename(staging)
        if str(blob.crc32c or "") and str(blob.crc32c) != _crc32c(staging):
            raise SystemExit(f"GCS download crc32c mismatch: {object_name}")
        md5 = hashlib.md5(staging.read_bytes()).hexdigest()  # noqa: S324
        ack = baidu.put_wal_ciphertext_if_absent(staging, object_name, metadata)
        if ack.size != size or ack.checksum.algo != MD5 or ack.checksum.value != md5:
            raise SystemExit(f"Baidu read-back differs from the GCS source: {object_name}")
    return {
        "object_name": object_name,
        "gcs_generation": str(blob.generation),
        "gcs_crc32c": str(blob.crc32c or ""),
        "baidu_pin": ack.pin_token,
        "size": str(size),
        "md5": md5,
    }


def _crc32c(path: Path) -> str:
    checksum = google_crc32c.Checksum()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return base64.b64encode(checksum.digest()).decode("ascii")


def _mapping_rows(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["object_name"] in mapping:
            raise SystemExit(f"duplicate object name in mapping: {row['object_name']}")
        mapping[row["object_name"]] = row
    return mapping


# ── local record rewrite ──


def _rewrite_object_identity(
    raw: dict[str, Any],
    object_name: str,
    mapping: dict[str, dict[str, str]],
    *,
    checksum_prefix: str,
) -> None:
    """Field-level rewrite of one identity-bearing record: keeps every
    other byte of the original serialization, swaps the GCS vocabulary for
    the Baidu one. ``checksum_prefix`` is empty for restore objects and
    ``ciphertext_`` for ACKs / candidate base objects."""
    row = mapping.get(object_name)
    if row is None:
        raise SystemExit(f"record references an object missing from the migration: {object_name}")
    raw.pop("generation", None)
    raw.pop("crc32c", None)
    raw["pin_token"] = row["baidu_pin"]
    raw[f"{checksum_prefix}checksum_algo"] = MD5
    raw[f"{checksum_prefix}checksum_value"] = row["md5"]


def _rewrite_candidate(raw: dict[str, Any], mapping: dict[str, dict[str, str]]) -> None:
    base = raw.get("base_object")
    if not isinstance(base, dict):
        raise SystemExit("candidate manifest lacks a base object")
    _rewrite_object_identity(
        base, str(base.get("object_name")), mapping, checksum_prefix="ciphertext_"
    )
    raw["base_object"] = base
    container_name = raw.get("native_manifest_container_object_name")
    if container_name is not None:
        container_row = mapping.get(str(container_name))
        if container_row is None:
            raise SystemExit(
                f"candidate references a container missing from the migration: {container_name}"
            )
        raw.pop("native_manifest_container_generation", None)
        raw["native_manifest_container_pin_token"] = container_row["baidu_pin"]


def _rewrite_protected(raw: dict[str, Any], mapping: dict[str, dict[str, str]]) -> None:
    base = raw.get("base")
    if not isinstance(base, dict):
        raise SystemExit("protected manifest lacks a base object")
    _rewrite_object_identity(base, str(base.get("object_name")), mapping, checksum_prefix="")
    raw["base"] = base
    for item in cast(list[Any], raw.get("wal") or []):
        if not isinstance(item, dict):
            raise SystemExit("protected manifest WAL entry is not an object")
        _rewrite_object_identity(item, str(item.get("object_name")), mapping, checksum_prefix="")
    candidate = raw.get("candidate")
    if isinstance(candidate, dict):
        _rewrite_candidate(candidate, mapping)
        raw["candidate"] = candidate
        # The digest pins the embedded candidate's canonical bytes; the
        # identity rewrite changes those bytes, so the digest must follow
        # or ProtectedManifest.from_json refuses the record (QA #1155).
        parsed = CandidateManifest.from_json(
            json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        )
        raw["candidate_sha256"] = candidate_sha256(parsed)


def _rewrite_ack(raw: dict[str, Any], mapping: dict[str, dict[str, str]]) -> None:
    _rewrite_object_identity(
        raw, str(raw.get("object_name")), mapping, checksum_prefix="ciphertext_"
    )


def _rewrite_records(
    records_root: Path, mapping: dict[str, dict[str, str]], *, dry_run: bool
) -> None:
    """Two-phase: every identity lookup resolves and every rewritten
    record re-parses before a single file is written, so a partial
    mapping or a vocabulary slip can never leave half-rewritten or
    unreadable records."""
    planned: list[tuple[Path, str]] = []
    for name in _RECORD_DIRS:
        directory = records_root / name
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            raw: object = json.loads(path.read_text())
            if not isinstance(raw, dict):
                raise SystemExit(f"record is not an object: {path}")
            if name == "ack":
                _rewrite_ack(raw, mapping)
            elif name in ("base-manifests",):
                _rewrite_candidate(raw, mapping)
            else:
                _rewrite_protected(raw, mapping)
            content = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            # Fail-closed re-parse: the post-cut drill reads these records
            # through the same parsers — a rewrite the parsers refuse must
            # abort here, before any file is written (QA #1155).
            try:
                if name == "ack":
                    ack_manifest_from_raw(json.loads(content))
                elif name in ("base-manifests",):
                    CandidateManifest.from_json(content)
                else:
                    ProtectedManifest.from_json(content)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SystemExit(f"rewritten record does not re-parse: {path} ({exc})") from exc
            planned.append((path, content))
    if dry_run:
        print(f"[dry-run] {len(planned)} records would be rewritten")
        return
    for path, content in planned:
        path.write_text(content)
        path.chmod(0o600)
    print(f"{len(planned)} records rewritten")


def main() -> None:
    args = _parse_args()
    _preflight(args.records_root)
    gcs, baidu = _client(args)

    mapping_path = args.mapping or args.records_root / "migration-mapping.json"
    snapshot_path = (
        args.snapshot
        or args.records_root
        / f"migration-snapshot-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    )
    if not args.dry_run:
        _snapshot(args.records_root, snapshot_path)

    blobs = _blobs(gcs, args)
    print(f"migrating {len(blobs)} objects from gs://{args.gcs_bucket}/{args.gcs_prefix}/ ...")
    rows: list[dict[str, str]] = []
    total_bytes = 0
    started = datetime.now(UTC)
    for blob in blobs:
        object_name = str(blob.name)
        metadata = dict(blob.metadata or {})
        size = int(blob.size)
        total_bytes += size
        row = _migrate_object(
            baidu=baidu, blob=blob, object_name=object_name, size=size, metadata=metadata
        )
        rows.append(row)
        mapping_path.write_text(
            json.dumps(rows, indent=2, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        print(f"  {row['object_name']}: {size} bytes -> {row['baidu_pin']}")

    _rewrite_records(args.records_root, _mapping_rows(rows), dry_run=args.dry_run)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(
        f"migration complete: {len(rows)} objects, {total_bytes} bytes, "
        f"{elapsed:.1f}s; mapping: {mapping_path}"
    )


if __name__ == "__main__":
    main()
