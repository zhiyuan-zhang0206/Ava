"""Inspect the editable Ava install in a checkout's virtualenv.

uv writes ``_editable_impl_ava.pth`` into the active virtualenv, and records
the editable source URL in the matching ``*.dist-info/direct_url.json``. A
polluted ``VIRTUAL_ENV`` can therefore make a worktree install repoint a
long-lived checkout's interpreter at disposable source. This module only reads
the install: the exec boundary refuses to spawn a child from a violating
interpreter and the health probe reports the violation. The few file stats are
negligible beside a process spawn, and deliberately remain uncached so a newly
poisoned install cannot evade the next execution. Nothing here rewrites an
install; the fix for a violation is a locked ``uv sync`` run by the operator.

A failed sync that removes either the pointer or its ``direct_url.json``
metadata is a half-uninstall, not a missing-install no-op, and is reported as a
violation.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast

EDITABLE_PTH_NAME = "_editable_impl_ava.pth"
EDITABLE_DIST_INFO_GLOB = "ava-*.dist-info/direct_url.json"


def editable_ava_pth_paths(source_root: Path) -> tuple[Path, ...]:
    """Existing Ava editable pointers under ``source_root/.venv``.

    Virtualenvs use ``lib/pythonX.Y/site-packages`` (or ``lib64``). Scan those
    layouts explicitly rather than deriving one from the running interpreter:
    stale Python-version directories are validated together rather than
    leaving a dormant poisoned pointer.
    """

    venv = source_root / ".venv"
    matches: set[Path] = set()
    for pattern in (
        f"lib/python*/site-packages/{EDITABLE_PTH_NAME}",
        f"lib64/python*/site-packages/{EDITABLE_PTH_NAME}",
    ):
        matches.update(venv.glob(pattern))
    return tuple(sorted(matches, key=str))


def editable_direct_url_paths(source_root: Path) -> tuple[Path, ...]:
    """Existing editable ``direct_url.json`` records under ``source_root/.venv``.

    Same layout scan as :func:`editable_ava_pth_paths`: uv writes this file
    beside the editable pointer, so a poisoned install is recorded in both
    places and both must be asserted.
    """

    venv = source_root / ".venv"
    matches: set[Path] = set()
    for pattern in (
        f"lib/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
        f"lib64/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
    ):
        matches.update(venv.glob(pattern))
    return tuple(sorted(matches, key=str))


def _missing_editable_ava_pth_paths(source_root: Path) -> tuple[Path, ...]:
    """Pointer paths missing beside existing Ava editable metadata records."""

    paths = {
        record.parent.parent / EDITABLE_PTH_NAME
        for record in editable_direct_url_paths(source_root)
    }
    return tuple(sorted((path for path in paths if not path.exists()), key=str))


def _pth_paths_without_sibling_direct_url(source_root: Path) -> tuple[Path, ...]:
    """Pointers whose site-packages directory has no Ava direct-URL record."""

    missing = (
        pth_path
        for pth_path in editable_ava_pth_paths(source_root)
        if not tuple(pth_path.parent.glob(EDITABLE_DIST_INFO_GLOB))
    )
    return tuple(sorted(missing, key=str))


def _normalized_exact_path(path: Path) -> str:
    """Platform-native exact path identity."""

    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _pth_content_is_allowed(raw_text: str, allowed_roots: frozenset[str]) -> bool:
    """Whether every editable-pointer line names an allowlisted exact root.

    uv writes one line for each wheel package, so a healthy project with
    multiple packages can legitimately repeat the same checkout path. Pointer
    legality is therefore semantic rather than a particular byte layout.
    """

    targets = raw_text.splitlines()
    if not targets or any(not target for target in targets):
        return False
    try:
        return all(_normalized_exact_path(Path(target)) in allowed_roots for target in targets)
    except (OSError, ValueError):
        return False


def _direct_url_is_allowed(raw_text: str, allowed_urls: frozenset[str]) -> bool:
    """A ``direct_url.json`` record is legal when it names an allowed root as
    an editable file URL.

    Anything unparsable, pointing at a non-allowlisted root, or not marked
    editable is treated as poisoned: the editable pointer in the same venv
    proves this is an editable install, so a record that disagrees is stale
    and a later ``uv sync`` could install from it.
    """

    if not raw_text.strip():
        return False
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    payload = cast("dict[str, object]", payload)
    url = payload.get("url")
    dir_info = payload.get("dir_info")
    if not isinstance(url, str) or not isinstance(dir_info, dict):
        return False
    if cast("dict[str, object]", dir_info).get("editable") is not True:
        return False
    return url.rstrip("/") in allowed_urls


def editable_install_violations(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    """Human-readable allowlist violations of a checkout's editable install.

    Discovers every record (the ``.pth`` pointer and every ``direct_url.json``)
    and validates them against the exact-root allowlist; it never writes. The
    health probe and the exec boundary report through this one function. A record
    that cannot be read is reported as a violation rather than crashing the
    caller.

    Returns an empty tuple when every record is legal. Both editable records
    missing is a no-op, while a missing pointer or metadata beside its existing
    sibling is a half-uninstall violation.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    normalized_allowed = frozenset(
        {_normalized_exact_path(resolved_source)}
        | {_normalized_exact_path(root) for root in allowed_roots}
    )
    allowed_urls = frozenset(
        {resolved_source.as_uri()}
        | {root.expanduser().resolve(strict=False).as_uri() for root in allowed_roots}
    )
    violations: list[str] = []
    for pth_path in _missing_editable_ava_pth_paths(resolved_source):
        violations.append(
            f"{pth_path} editable install metadata present but pointer missing — half-uninstalled"
        )
    for pth_path in _pth_paths_without_sibling_direct_url(resolved_source):
        violations.append(
            f"{pth_path} editable pointer present but metadata missing — half-uninstalled"
        )
    for pth_path in editable_ava_pth_paths(resolved_source):
        try:
            raw_text = pth_path.read_text()
        except OSError:
            violations.append(f"{pth_path} unreadable")
            continue
        if not _pth_content_is_allowed(raw_text, normalized_allowed):
            target = raw_text.strip() or "(empty)"
            violations.append(
                f"{pth_path} names {target!r}; non-allowlisted source or is inconsistent"
            )
    for record in editable_direct_url_paths(resolved_source):
        try:
            raw_text = record.read_text()
        except OSError:
            violations.append(f"{record} unreadable")
            continue
        if not _direct_url_is_allowed(raw_text, allowed_urls):
            violations.append(f"{record} records {raw_text.strip() or '(empty)'!r}")
    return tuple(sorted(violations))


def venv_python(source_root: Path) -> Path | None:
    """First existing checkout virtualenv interpreter."""

    venv = source_root / ".venv"
    for candidate in (
        venv / "bin" / "python3",
        venv / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return None


def editable_console_script_violations(source_root: Path) -> tuple[str, ...]:
    """Report a missing Ava console script beside the virtualenv's interpreter."""

    interpreter = venv_python(source_root)
    if interpreter is None:
        return ()
    console_script = interpreter.with_name("ava")
    if console_script.exists():
        return ()
    return (f"{console_script} editable console script missing",)


def current_interpreter_source_root() -> Path | None:
    """Return this interpreter's checkout root when it is under ``.venv``.

    The supported layout is ``<root>/.venv/bin/python``. A system interpreter or shim carries
    no checkout authority, so callers deliberately treat it as a no-op.
    """

    # A virtualenv's Python is normally a symlink to its base interpreter. The
    # lexical executable path carries the ``.venv`` identity; resolving it
    # before the layout check would lose that boundary.
    interpreter = Path(sys.executable)
    try:
        venv = interpreter.parents[1]
        source_root = interpreter.parents[2]
    except IndexError:
        return None
    if venv.name != ".venv" or interpreter.parent.name != "bin":
        return None
    return source_root.resolve(strict=False)
