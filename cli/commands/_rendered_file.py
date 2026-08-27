"""Converge-rendered file writes with content-hash user-modification protection.

Precedent: the web-sources converge protection (cli/commands/_converge_skills.py)
— a converge-managed copy whose destination hash no longer matches the recorded
hash was hand-edited by the user, so converge warns and preserves it instead of
overwriting. Used by the LGTM provisioning renderer and the otel-collector
config renderer (task #1791, A3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path


def write_rendered_guarded(
    path: Path,
    content: str,
    hashes_path: Path,
    key: str,
    *,
    writer: Callable[[Path, str], None] | None = None,
) -> str | None:
    """Write converge-rendered ``content`` to ``path`` unless the file was
    hand-edited since the last converge write.

    The sidecar ``hashes_path`` (JSON: key -> sha256 of what converge last
    wrote) decides: a destination whose current hash differs from the recorded
    one was modified locally, so converge warns and preserves it — nothing is
    written and the recorded hash is left untouched (the warning repeats on
    every converge until the user removes the file or re-runs converge on a
    clean copy). A destination with no record (first converge after the
    tracking feature landed) is adopted and re-recorded.

    ``writer`` overrides how the content lands on disk (e.g. an atomic
    owner-only write for secret-bearing configs); the default is a plain
    ``write_text``.

    Returns the warning string when the file was preserved, else None.
    """
    recorded: dict[str, str] = {}
    if hashes_path.exists():
        recorded = json.loads(hashes_path.read_text(encoding="utf-8"))
    if path.exists() and key in recorded:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != recorded[key]:
            return (
                f"{path} was modified locally; not overwritten "
                f"(remove it and re-run converge to restore from the rendered output)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    if writer is None:
        path.write_text(content, encoding="utf-8")
    else:
        writer(path, content)
    recorded[key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    return None
