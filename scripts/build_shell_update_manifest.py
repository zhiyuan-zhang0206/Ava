#!/usr/bin/env python3
"""Build Tauri's static ``latest.json`` from shell release artifacts.

The desktop updater consumes signed ``.app.tar.gz`` and ``.nsis.zip``
archives, not the user-facing DMG/EXE installers. A release built without the
optional updater key still gets a valid manifest with no platform entries: the
release pipeline remains testable, while clients correctly find no installable
update rather than being handed an unverifiable archive.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

_TAG = re.compile(r"shell-v(?P<version>\d+\.\d+\.\d+)\Z")


def _signed_archive(artifacts: Path, pattern: str, label: str) -> tuple[Path, str] | None:
    signed: list[tuple[Path, str]] = []
    for archive in sorted(artifacts.glob(pattern)):
        signature_path = archive.with_name(f"{archive.name}.sig")
        if not signature_path.is_file():
            continue
        signature = signature_path.read_text(encoding="utf-8").strip()
        if signature:
            signed.append((archive, signature))
    if len(signed) > 1:
        names = ", ".join(archive.name for archive, _ in signed)
        raise ValueError(f"multiple {label} updater archives: {names}")
    return signed[0] if signed else None


def _platform_entry(repo: str, tag: str, archive: Path, signature: str) -> dict[str, str]:
    asset = quote(archive.name, safe="")
    return {
        "url": f"https://github.com/{repo}/releases/download/{tag}/{asset}",
        "signature": signature,
    }


def build_manifest(*, tag: str, artifacts: Path, repo: str) -> dict[str, Any]:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"'{tag}' is not a shell-v<major>.<minor>.<patch> tag")
    if len(repo.split("/")) != 2 or any(not part for part in repo.split("/")):
        raise ValueError(f"'{repo}' is not an owner/repository name")
    if not artifacts.is_dir():
        raise ValueError(f"artifact directory does not exist: {artifacts}")

    platforms: dict[str, dict[str, str]] = {}
    macos = _signed_archive(artifacts, "*.app.tar.gz", "macOS")
    if macos is not None:
        archive, signature = macos
        entry = _platform_entry(repo, tag, archive, signature)
        # The universal archive contains both slices; the updater identifies
        # the running slice, so both target keys point at the same asset.
        platforms["darwin-aarch64"] = entry
        platforms["darwin-x86_64"] = entry

    windows = _signed_archive(artifacts, "*.nsis.zip", "Windows")
    if windows is not None:
        archive, signature = windows
        entry = _platform_entry(repo, tag, archive, signature)
        # Updater 2 prefers the installer-qualified key and falls back to the
        # unqualified target. Publishing both keeps the feed compatible with
        # clients from either side of that change.
        platforms["windows-x86_64-nsis"] = entry
        platforms["windows-x86_64"] = entry

    return {"version": match.group("version"), "platforms": platforms}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = build_manifest(tag=args.tag, artifacts=args.artifacts, repo=args.repo)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
