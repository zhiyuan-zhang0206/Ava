"""Baidu Netdisk PITR speed test for the P0 live smoke.

Runs against the REAL PCS API with a real OAuth pair, so it needs the
operator's credential file and an already-refreshed token state file
(see services/pitr/baidu_token.py). It exercises the three-phase upload
of a synthetic file, rapid transfer on re-upload, and a verified
download — reporting wall-clock throughput for each phase.

Usage:
  python scripts/pitr_baidu_speedtest.py \
      --credentials ~/.ava/physical-backup/baidu-credentials.json \
      --token ~/.ava/physical-backup/baidu-token.json \
      --app-root /apps/ava/ava-pitr --prefix ava-pitr --size-mb 256

The object is written under <app-root>/<prefix>/speedtest/<timestamp>.bin
and DELETED afterwards (the sidecar too).
"""

from __future__ import annotations

import argparse
import hashlib
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from services.pitr.baidu_restore_store import BaiduGenerationPinnedObjectReader
from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.baidu_token import BaiduCredentials, BaiduTokenManager
from services.pitr.restore_manifest import RestoreObject


def _measure(label: str, size_bytes: int, start: float) -> None:
    elapsed = time.monotonic() - start
    mebibytes = size_bytes / (1024 * 1024)
    print(f"{label}: {mebibytes:.1f} MiB in {elapsed:.2f}s = {mebibytes / elapsed:.2f} MiB/s")


def _write_payload(path: Path, size: int) -> str:
    """Deterministic payload; returns its md5 hex."""
    block = hashlib.sha256(b"baidu-speedtest").digest() * 4096
    with path.open("wb") as output:
        remaining = size
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            output.write(chunk)
            remaining -= len(chunk)
    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 — payload digest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--token", required=True, type=Path)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--size-mb", default=256, type=int)
    parser.add_argument("--keep", action="store_true", help="keep the uploaded object")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    credentials = BaiduCredentials(args.credentials)
    token_manager = BaiduTokenManager(credentials, args.token)
    print("access token:", token_manager.get_access_token()[:8] + "...")
    health = token_manager.health()
    print("token health:", health.remaining_seconds, health.expires_at, health.refresh_error)

    store = BaiduObjectStore(
        app_root=args.app_root, token_manager=token_manager, timeout_seconds=600
    )
    reader = BaiduGenerationPinnedObjectReader(
        app_root=args.app_root, token_manager=token_manager, timeout_seconds=600
    )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    object_name = f"{args.prefix}/speedtest/{stamp}.bin"
    size = args.size_mb * 1024 * 1024
    print(f"building {args.size_mb} MiB of deterministic payload ...")
    with tempfile.TemporaryDirectory(prefix="baidu-speedtest-") as scratch:
        path = Path(scratch) / "payload.bin"
        digest = _write_payload(path, size)

        print(f"uploading {object_name} (md5={digest[:12]}...) ...")
        started = time.monotonic()
        ack = store.put_wal_ciphertext_if_absent(path, object_name, {"ava-speedtest": "1"})
        _measure("upload", size, started)
        print(
            "ack:",
            ack.pin_token,
            ack.checksum.algo,
            ack.checksum.value[:12],
            "created:",
            ack.created,
        )

        print("re-uploading the same content (rapid transfer expected) ...")
        started = time.monotonic()
        again = store.put_wal_ciphertext_if_absent(path, object_name, {"ava-speedtest": "1"})
        _measure("re-upload", size, started)
        print(
            "re-upload ack created:", again.created, "(False = rapid transfer adopted the object)"
        )
        if again.created:
            raise SystemExit("rapid transfer did not adopt the identical content — investigate")

        print("verifying stat ...")
        observed = store.stat(object_name)
        print("stat:", observed.pin_token if observed else None)

        print("downloading to verify ...")
        expected = RestoreObject(
            "speedtest",
            object_name,
            ack.pin_token,
            size,
            ack.checksum.algo,
            ack.checksum.value,
            (("ava-speedtest", "1"),),
        )
        destination = Path(scratch) / "downloaded.bin"
        started = time.monotonic()
        reader.download_exact(expected, destination)
        _measure("download", size, started)
        if hashlib.md5(destination.read_bytes()).hexdigest() != digest:  # noqa: S324
            raise SystemExit("downloaded content differs from the uploaded payload")
        print("download content verified")

    if not args.keep:
        print("deleting the object and its sidecar ...")
        client = store._client()
        client.delete_files(
            [f"{args.app_root}/{object_name}", f"{args.app_root}/{object_name}.ack.json"]
        )
    print("speed test complete")


if __name__ == "__main__":
    main()
