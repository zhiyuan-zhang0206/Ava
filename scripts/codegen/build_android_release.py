#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

BUILD_NUMBER_BASE = 1_000_000
LEGACY_VERSION_CODE = 4002
LATEST_RELEASE_TAG = "android-latest"
MANIFEST_NAME = "android-latest.json"

_VERSION_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def build_version_code(run_number: int) -> int:
    if run_number < 1:
        raise ValueError(f"run number must be positive, got {run_number}")
    return _checked_version_code(BUILD_NUMBER_BASE + run_number)


def tauri_config(*, version_name: str, version_code: int) -> str:
    return json.dumps(
        {
            "version": _checked_version_name(version_name),
            "bundle": {
                "android": {
                    "versionCode": _checked_version_code(version_code),
                    "autoIncrementVersionCode": False,
                }
            },
        }
    )


def build_manifest(
    *,
    version_code: int,
    version_name: str,
    apk: Path,
    commit: str,
    repo: str,
    built_at: str | None = None,
) -> dict[str, Any]:
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError(f"'{commit}' is not a full commit SHA")
    parts = repo.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError(f"'{repo}' is not an owner/repository name")
    if not apk.is_file():
        raise ValueError(f"apk does not exist: {apk}")
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "versionCode": _checked_version_code(version_code),
        "versionName": _checked_version_name(version_name),
        "apkUrl": (
            f"https://github.com/{repo}/releases/download/{LATEST_RELEASE_TAG}/"
            f"{quote(apk.name, safe='')}"
        ),
        "sha256": digest,
        "commit": commit,
        "builtAt": _utc_timestamp(built_at),
    }


def assert_version_code_advances(*, current: Path, version_code: int) -> None:
    body: Any = json.loads(current.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise TypeError(f"{current} is not a manifest object")
    previous = body.get("versionCode")
    if isinstance(previous, bool) or not isinstance(previous, int):
        raise TypeError(f"{current} has no integer versionCode")
    if version_code <= previous:
        raise ValueError(
            f"refusing versionCode {version_code}: android-latest already advertises {previous}"
        )


def _checked_version_code(version_code: int) -> int:
    if version_code <= LEGACY_VERSION_CODE:
        raise ValueError(
            f"versionCode {version_code} must exceed the legacy code {LEGACY_VERSION_CODE}"
        )
    return version_code


def _checked_version_name(version_name: str) -> str:
    if _VERSION_NAME.fullmatch(version_name) is None:
        raise ValueError(f"'{version_name}' cannot be used as a versionName")
    return version_name


def _utc_timestamp(value: str | None) -> str:
    moment = datetime.now(UTC) if value is None else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"'{value}' must carry a UTC offset")
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Android release identity: the injected versionCode, the Tauri --config "
            "overlay and the android-latest.json update manifest."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    code_parser = commands.add_parser("version-code", help="print the injected versionCode")
    code_parser.add_argument("--run-number", type=int, required=True)

    config_parser = commands.add_parser("tauri-config", help="print the Tauri --config overlay")
    config_parser.add_argument("--version-name", required=True)
    config_parser.add_argument("--version-code", type=int, required=True)

    guard_parser = commands.add_parser(
        "assert-advances", help="fail unless the code is newer than android-latest's manifest"
    )
    guard_parser.add_argument("--current", type=Path, required=True)
    guard_parser.add_argument("--version-code", type=int, required=True)

    manifest_parser = commands.add_parser("manifest", help=f"write {MANIFEST_NAME}")
    manifest_parser.add_argument("--version-code", type=int, required=True)
    manifest_parser.add_argument("--version-name", required=True)
    manifest_parser.add_argument("--apk", type=Path, required=True)
    manifest_parser.add_argument("--commit", required=True)
    manifest_parser.add_argument("--repo", required=True)
    manifest_parser.add_argument("--built-at", help="ISO-8601 timestamp with offset (default: now)")
    manifest_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "version-code":
        print(build_version_code(args.run_number))
        return 0
    if args.command == "tauri-config":
        print(tauri_config(version_name=args.version_name, version_code=args.version_code))
        return 0
    if args.command == "assert-advances":
        assert_version_code_advances(current=args.current, version_code=args.version_code)
        print(f"versionCode {args.version_code} advances android-latest")
        return 0

    body = build_manifest(
        version_code=args.version_code,
        version_name=args.version_name,
        apk=args.apk,
        commit=args.commit,
        repo=args.repo,
        built_at=args.built_at,
    )
    text = json.dumps(body, indent=2) + "\n"
    args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
