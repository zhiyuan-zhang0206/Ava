"""Regional mirror transport must never become the committed npm lock."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.lint.locks import package_lock as gate

_CANONICAL = "https://registry.npmjs.org/example/-/example-1.0.0.tgz"


def _lock(path: Path, *, resolved: str | None) -> Path:
    entry: dict[str, object] = {"version": "1.0.0", "integrity": "sha512-abc"}
    if resolved is not None:
        entry["resolved"] = resolved
    path.write_text(
        json.dumps(
            {"lockfileVersion": 3, "packages": {"": {"name": "x"}, "node_modules/example": entry}}
        ),
        encoding="utf-8",
    )
    return path


def test_rejects_a_mirror_resolved_url(tmp_path: Path) -> None:
    path = _lock(
        tmp_path / "package-lock.json",
        resolved="https://registry.npmmirror.com/example/-/example-1.0.0.tgz",
    )
    assert len(gate.violations(path)) == 1


def test_accepts_canonical_and_resolvedless_entries(tmp_path: Path) -> None:
    path = _lock(tmp_path / "package-lock.json", resolved=_CANONICAL)
    assert gate.violations(path) == []
    path = _lock(tmp_path / "package-lock.json", resolved=None)
    assert gate.violations(path) == []
