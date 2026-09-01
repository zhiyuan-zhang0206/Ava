"""Refuse a worktree sync when its virtualenv can write outside the checkout.

This module depends only on the standard library so it can run before uv builds
or imports a project environment. It validates the same POSIX, lib64, and
Windows editable-install layouts as ``shared.editable_install`` without
importing project dependencies.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

_EDITABLE_PTH_NAME = "_editable_impl_ava.pth"
_EDITABLE_DIST_INFO_GLOB = "ava-*.dist-info/direct_url.json"
_RECORD_PATTERNS = (
    f"Lib/site-packages/{_EDITABLE_PTH_NAME}",
    f"lib/python*/site-packages/{_EDITABLE_PTH_NAME}",
    f"lib64/python*/site-packages/{_EDITABLE_PTH_NAME}",
)
_DIRECT_URL_PATTERNS = (
    f"Lib/site-packages/{_EDITABLE_DIST_INFO_GLOB}",
    f"lib/python*/site-packages/{_EDITABLE_DIST_INFO_GLOB}",
    f"lib64/python*/site-packages/{_EDITABLE_DIST_INFO_GLOB}",
)


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(_resolved(left))) == os.path.normcase(str(_resolved(right)))


def _inside(path: Path, root: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(root))
    except ValueError:
        return False
    return True


def _record_paths(checkout: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    matches: set[Path] = set()
    for pattern in patterns:
        matches.update((checkout / ".venv").glob(pattern))
    return tuple(sorted(matches, key=str))


def _editable_pth_violations(checkout: Path) -> list[str]:
    violations: list[str] = []
    for path in _record_paths(checkout, _RECORD_PATTERNS):
        try:
            raw_target = path.read_text().strip()
        except OSError as exc:
            violations.append(f"{path} cannot be read: {exc}")
            continue
        try:
            targets = raw_target.splitlines()
            target_matches_checkout = bool(targets) and all(
                target and _same_path(Path(target), checkout) for target in targets
            )
        except (OSError, ValueError):
            target_matches_checkout = False
        if not target_matches_checkout:
            violations.append(
                f"{path} names {raw_target or '(empty)'!r}, not checkout {_resolved(checkout)}"
            )
    return violations


def _direct_url_violations(checkout: Path) -> list[str]:
    violations: list[str] = []
    expected_url = _resolved(checkout).as_uri()
    for path in _record_paths(checkout, _DIRECT_URL_PATTERNS):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            violations.append(f"{path} has unreadable direct_url.json: {exc}")
            continue
        if not isinstance(payload, dict):
            violations.append(f"{path} records {payload!r}, not editable checkout {expected_url}")
            continue
        payload = cast("dict[str, object]", payload)
        url = payload.get("url")
        dir_info = payload.get("dir_info")
        editable = (
            isinstance(dir_info, dict)
            and cast("dict[str, object]", dir_info).get("editable") is True
        )
        if url != expected_url or not editable:
            violations.append(f"{path} records {payload!r}, not editable checkout {expected_url}")
    return violations


def check_checkout(checkout: Path, env: Mapping[str, str]) -> list[str]:
    """Return every condition that makes a worktree's next uv sync unsafe."""

    resolved_checkout = _resolved(checkout)
    venv = checkout / ".venv"
    violations: list[str] = []
    if venv.is_symlink():
        violations.append(
            f"{venv} is a symlink resolving to {_resolved(venv)}; worktree .venv must be a real directory"
        )
    elif venv.exists() and not _inside(venv, resolved_checkout):
        violations.append(f"{venv} resolves outside checkout to {_resolved(venv)}")

    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env and not _inside(Path(virtual_env), resolved_checkout):
        violations.append(
            f"VIRTUAL_ENV={virtual_env!r} resolves outside checkout to {_resolved(Path(virtual_env))}; "
            "rerun with env -u VIRTUAL_ENV"
        )

    violations.extend(_editable_pth_violations(resolved_checkout))
    violations.extend(_direct_url_violations(resolved_checkout))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Print structured violations and return 1, or 0 when a sync is safe."""

    args = tuple(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("usage: guard_editable_venv.py [checkout-root]", file=sys.stderr)
        return 2
    checkout = Path(args[0]) if args else Path.cwd()
    violations = check_checkout(checkout, os.environ)
    if not violations:
        return 0
    print("guard_editable_venv: refusing uv sync:", file=sys.stderr)
    for violation in violations:
        print(f"- {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
