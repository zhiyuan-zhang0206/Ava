"""Read-only source identity for a development root generation.

The snapshot includes dirty and untracked source, not just HEAD. Ignored build
outputs and the editable environment are not sealed by this development check;
production release admission must verify its complete immutable artifact.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

_STABLE_STAT = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


def source_digest(repo: Path) -> str:
    """Bind the names, modes and bytes Git would include, without changing Git."""
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30)
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    names = sorted(set(result.stdout.split(b"\0")) - {b""})
    digest = hashlib.sha256(head)
    for encoded in names:
        path = repo / encoded.decode("utf-8")
        digest.update(encoded + b"\0")
        try:
            before = path.lstat()
            mode = before.st_mode
        except FileNotFoundError:
            digest.update(b"deleted\0")
            continue
        if stat.S_ISLNK(mode):
            content = str(path.readlink()).encode()
        elif stat.S_ISREG(mode):
            content = path.read_bytes()
        else:
            raise RuntimeError(f"unsupported development source member: {path}")
        after = path.lstat()
        if any(getattr(before, key) != getattr(after, key) for key in _STABLE_STAT):
            raise RuntimeError(f"development source changed while reading: {path}")
        digest.update(str(mode).encode() + b"\0" + hashlib.sha256(content).digest())
    if subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30) != head:
        raise RuntimeError("development HEAD changed while reading source")
    return digest.hexdigest()


def configuration_digest(home: Path) -> str:
    """Include file-authoritative values omitted from child environment transport."""
    paths = [home / ".env", home / "plugins_config.json"]
    paths.extend(sorted((home / "configs").glob("*/config.json")))
    files = {
        str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
        if path.exists()
        else None
        for path in paths
    }
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def launch_digest(repo: Path, environment: dict[str, str], *, home: Path) -> str:
    """Bind source, transport environment and authoritative on-disk configuration."""
    payload = {
        "source": source_digest(repo),
        "environment": environment,
        "configuration": configuration_digest(home),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
