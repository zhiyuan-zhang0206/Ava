"""Expose Ava's operator context to already-installed external agent clients.

Codex and Claude Code own their global directories. Ava therefore manages one
copied skill target inside each existing client home and proves ownership with a
content digest before replacing it. Nothing else under either client home is a
converge surface.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import cast

from cli.commands._converge_spec import ConvergeCtx

_CLIENT_HOME_NAMES = (".codex", ".claude")
_SKILL_NAME = "operating-ava-cluster"
_MARKER_NAME = ".ava-managed.json"
_MARKER_FORMAT = 1


class _UnsafeSkillTreeError(RuntimeError):
    """A skill tree contains an entry a portable copied package cannot own."""


def _tree_digest(root: Path) -> str:
    """Hash relative names, entry kinds, and file bytes without following links."""
    digest = hashlib.sha256()

    def add_field(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, byteorder="big"))
        digest.update(value)

    def add_directory(directory: Path) -> None:
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            if entry == root / _MARKER_NAME:
                continue
            relative = entry.relative_to(root).as_posix().encode()
            if entry.is_symlink():
                raise _UnsafeSkillTreeError(f"symbolic link is not portable: {relative.decode()}")
            if entry.is_dir():
                digest.update(b"directory")
                add_field(relative)
                add_directory(entry)
                continue
            if not entry.is_file():
                raise _UnsafeSkillTreeError(f"unsupported filesystem entry: {relative.decode()}")
            digest.update(b"file")
            add_field(relative)
            file_digest = hashlib.sha256()
            with entry.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            digest.update(file_digest.digest())

    add_directory(root)
    return digest.hexdigest()


def _managed_digest(target: Path) -> str | None:
    marker = target / _MARKER_NAME
    if target.is_symlink() or not target.is_dir() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        parsed: object = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    record = cast(dict[str, object], parsed)
    content_digest = record.get("content_sha256")
    if (
        record.get("format") != _MARKER_FORMAT
        or record.get("owner") != "ava"
        or record.get("skill") != _SKILL_NAME
        or not isinstance(content_digest, str)
        or len(content_digest) != 64
    ):
        return None
    return content_digest


def _stage_copy(source: Path, skills_root: Path, content_digest: str) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=f".{_SKILL_NAME}.ava-stage-", dir=skills_root))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        marker = {
            "content_sha256": content_digest,
            "format": _MARKER_FORMAT,
            "owner": "ava",
            "skill": _SKILL_NAME,
        }
        (staging / _MARKER_NAME).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _activate_copy(staging: Path, target: Path, *, replacing: bool) -> None:
    if not replacing:
        staging.replace(target)
        return

    # A non-empty directory cannot be replaced in one portable rename. Move the
    # verified Ava-owned copy aside, activate the complete stage, then remove
    # only that exact old copy. Failure restores the previous target.
    previous = staging.with_name(f"{staging.name}.previous")
    target.replace(previous)
    try:
        staging.replace(target)
    except Exception:
        previous.replace(target)
        raise
    shutil.rmtree(previous)


def _converge_client(source: Path, client_home: Path, source_digest: str) -> None:
    if not client_home.exists():
        return
    if not client_home.is_dir():
        print(
            f"  ! external agent skill skipped: client home is not a directory: {client_home}",
            file=sys.stderr,
        )
        return

    skills_root = client_home / "skills"
    if skills_root.exists() and not skills_root.is_dir():
        print(
            f"  ! external agent skill skipped: skills root is not a directory: {skills_root}",
            file=sys.stderr,
        )
        return
    skills_root.mkdir(exist_ok=True)
    target = skills_root / _SKILL_NAME
    target_exists = target.exists() or target.is_symlink()
    recorded_digest = _managed_digest(target) if target_exists else None
    if target_exists and recorded_digest is None:
        print(
            f"  ! external agent skill preserved unmanaged target: {target}",
            file=sys.stderr,
        )
        return
    if recorded_digest is not None:
        try:
            actual_digest = _tree_digest(target)
        except (OSError, _UnsafeSkillTreeError):
            actual_digest = None
        if actual_digest != recorded_digest:
            print(
                f"  ! external agent skill preserved user-modified managed target: {target}",
                file=sys.stderr,
            )
            return
        if recorded_digest == source_digest:
            return

    staging = _stage_copy(source, skills_root, source_digest)
    try:
        _activate_copy(staging, target, replacing=recorded_digest is not None)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    action = "updated" if recorded_digest is not None else "installed"
    print(f"  · external agent skill {action}: {target}")


def converge_external_agent_skill(ctx: ConvergeCtx) -> None:
    """Copy the operator skill into present Codex and Claude Code homes."""
    host_home = Path.home()
    client_homes = tuple(
        host_home / name
        for name in _CLIENT_HOME_NAMES
        if (host_home / name).exists() or (host_home / name).is_symlink()
    )
    if not client_homes:
        return

    source = ctx.repo / ".agents" / "skills" / _SKILL_NAME
    if not source.is_dir():
        raise FileNotFoundError(f"operator skill source is missing: {source}")
    if (source / _MARKER_NAME).exists():
        raise _UnsafeSkillTreeError(f"operator skill source reserves {_MARKER_NAME}")
    source_digest = _tree_digest(source)
    for client_home in client_homes:
        _converge_client(source, client_home, source_digest)
