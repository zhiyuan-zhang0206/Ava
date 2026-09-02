"""Guard the editable Ava install in a checkout's virtualenv.

uv writes ``_editable_impl_ava.pth`` into the active virtualenv, and records
the editable source URL in the matching ``*.dist-info/direct_url.json``. A
polluted ``VIRTUAL_ENV`` can therefore make a worktree install repoint a
long-lived checkout's interpreter at disposable source. Lifecycle callers use
the repair primitives for the prod checkout only; update callers use the write
window so an operator's emergency read-only mode does not block a legitimate
sync. The exec boundary runs the read/repair guard for every child spawn: its
few file stats are negligible beside a process spawn, and deliberately remain
uncached so a newly poisoned install cannot evade the next execution.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import shared.telemetry
from shared.log import logger

EDITABLE_PTH_NAME = "_editable_impl_ava.pth"
EDITABLE_DIST_INFO_GLOB = "ava-*.dist-info/direct_url.json"


@dataclass(frozen=True)
class EditableInstallRepair:
    """One poisoned editable-install record replaced with the checkout it belongs to."""

    path: Path
    poisoned_target: str
    source_root: Path


def editable_ava_pth_paths(source_root: Path) -> tuple[Path, ...]:
    """Existing Ava editable pointers under ``source_root/.venv``.

    POSIX virtualenvs use ``lib/pythonX.Y/site-packages`` while Windows uses
    ``Lib/site-packages``. Scan those layouts explicitly rather than deriving
    one from the running platform: tests and WSL management can inspect a tree
    laid out for a different host, and stale Python-version directories should
    be repaired together rather than leaving a dormant poisoned pointer.
    """

    venv = source_root / ".venv"
    matches: set[Path] = set()
    for pattern in (
        f"Lib/site-packages/{EDITABLE_PTH_NAME}",
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
        f"Lib/site-packages/{EDITABLE_DIST_INFO_GLOB}",
        f"lib/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
        f"lib64/python*/site-packages/{EDITABLE_DIST_INFO_GLOB}",
    ):
        matches.update(venv.glob(pattern))
    return tuple(sorted(matches, key=str))


def editable_site_packages_dirs(source_root: Path) -> tuple[Path, ...]:
    """Existing site-packages directories under ``source_root/.venv``.

    Discover layouts structurally rather than through Ava's editable-install
    records. A partially failed sync can remove those records while leaving a
    read-only directory that a later sync must still be able to open.
    """

    venv = source_root / ".venv"
    matches: set[Path] = set()
    for pattern in (
        "Lib/site-packages",
        "lib/python*/site-packages",
        "lib64/python*/site-packages",
    ):
        matches.update(path for path in venv.glob(pattern) if path.is_dir())
    return tuple(sorted(matches, key=str))


def _normalized_exact_path(path: Path) -> str:
    """Platform-native exact path identity, including Windows case folding."""

    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _target_is_allowed(raw_target: str, allowed_roots: frozenset[str]) -> bool:
    targets = raw_target.splitlines()
    if not targets:
        return False
    for target in targets:
        if not target:
            return False
        try:
            normalized = _normalized_exact_path(Path(target))
        except (OSError, ValueError):
            return False
        if normalized not in allowed_roots:
            return False
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` through a same-directory temp file plus rename.

    A crash mid-write must never leave a half-written pointer: the next
    interpreter would parse a truncated target, which the allowlist rejects
    and the next converge pass repairs — but only after this process already
    failed to start on it.
    """

    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


@contextlib.contextmanager
def _write_window(paths: Iterable[Path]) -> Generator[None, None, None]:
    """Temporarily add owner-write to read-only files, then restore it.

    The exact original mode is restored in ``finally`` on both successful and
    failed writes. If a writer atomically replaces the file, the replacement
    receives the original protection too. Paths that disappear before entry
    are skipped because a recreated virtualenv has no prior mode to restore.
    """

    original_modes: dict[Path, int] = {}
    for path in paths:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & stat.S_IWUSR:
                continue
            original_modes[path] = mode
            path.chmod(mode | stat.S_IWUSR)
        except FileNotFoundError:
            continue
    try:
        yield
    finally:
        for path, mode in original_modes.items():
            with contextlib.suppress(FileNotFoundError):
                path.chmod(mode)


@contextlib.contextmanager
def editable_site_packages_write_window(source_root: Path) -> Generator[None, None, None]:
    """Temporarily make protected Ava site-packages directories owner-writable.

    POSIX blocks uv's atomic replacement at the directory boundary, not the
    read-only record file. Windows ACLs have a different permission model, so
    this is intentionally a no-op there.
    """

    if os.name == "nt":
        yield
        return
    with _write_window(editable_site_packages_dirs(source_root)):
        yield


@contextlib.contextmanager
def editable_pth_write_window(source_root: Path) -> Generator[None, None, None]:
    """Temporarily open protected Ava records and their directories, then restore.

    The exact original mode is restored in ``finally`` on both successful and
    failed syncs. Directory access covers uv's atomic replacement, while file
    access keeps direct repair compatible with the legacy file-level guard.
    """

    records = editable_ava_pth_paths(source_root) + editable_direct_url_paths(source_root)
    with editable_site_packages_write_window(source_root), _write_window(records):
        yield


def repair_editable_ava_pth(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[EditableInstallRepair, ...]:
    """Repair pointers outside the exact source-root allowlist.

    An allowlisted clone root is legal only as that exact path. Its
    ``.worktrees/*`` descendants remain disposable and are repaired.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    normalized_allowed = frozenset(
        {_normalized_exact_path(resolved_source)}
        | {_normalized_exact_path(root) for root in allowed_roots}
    )
    repairs: list[EditableInstallRepair] = []
    for pth_path in editable_ava_pth_paths(resolved_source):
        raw_target = pth_path.read_text().strip()
        if _target_is_allowed(raw_target, normalized_allowed):
            continue
        with editable_pth_write_window(resolved_source):
            _atomic_write_text(pth_path, str(resolved_source))
        repair = EditableInstallRepair(
            path=pth_path,
            poisoned_target=raw_target,
            source_root=resolved_source,
        )
        repairs.append(repair)
        shared.telemetry.emit(
            "telemetry",
            "editable_pth_repaired",
            level="warning",
            source="converge",
            attributes={
                "pth_path": str(pth_path),
                "poisoned_target": raw_target,
                "source_root": str(resolved_source),
            },
        )
    return tuple(repairs)


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


def repair_editable_direct_url(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[EditableInstallRepair, ...]:
    """Repair ``direct_url.json`` records outside the exact source-root allowlist.

    The pointer and the URL must agree: a URL naming a worktree is the same
    poison recorded elsewhere, and a later ``uv sync`` inside the venv would
    install from it.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    allowed_urls = frozenset(
        {resolved_source.as_uri()}
        | {root.expanduser().resolve(strict=False).as_uri() for root in allowed_roots}
    )
    repairs: list[EditableInstallRepair] = []
    for path in editable_direct_url_paths(resolved_source):
        raw_text = path.read_text()
        if _direct_url_is_allowed(raw_text, allowed_urls):
            continue
        repaired = json.dumps(
            {"url": resolved_source.as_uri(), "dir_info": {"editable": True}},
            separators=(",", ":"),
        )
        with editable_pth_write_window(resolved_source):
            _atomic_write_text(path, repaired)
        repair = EditableInstallRepair(
            path=path,
            poisoned_target=raw_text.strip() or "(empty)",
            source_root=resolved_source,
        )
        repairs.append(repair)
        shared.telemetry.emit(
            "telemetry",
            "editable_direct_url_repaired",
            level="warning",
            source="converge",
            attributes={
                "direct_url_path": str(path),
                "poisoned_target": raw_text.strip() or "(empty)",
                "source_root": str(resolved_source),
            },
        )
    return tuple(repairs)


def repair_editable_install(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[EditableInstallRepair, ...]:
    """Repair every poisoned editable-install record (pointer + ``direct_url``)."""

    return repair_editable_ava_pth(
        source_root, allowed_roots=allowed_roots
    ) + repair_editable_direct_url(source_root, allowed_roots=allowed_roots)


def editable_install_violations(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    """Human-readable allowlist violations of a checkout's editable install.

    The read-only twin of the repair primitives: discovers the same records
    (the ``.pth`` pointer and every ``direct_url.json``) and validates them
    against the same exact-root allowlist, but never writes. The health probe
    reports through this function so its detection can never drift from the
    converge guard's repair semantics. A record that cannot be read is
    reported as a violation rather than crashing the caller.

    Returns an empty tuple when every record is legal — a missing record is a
    no-op, matching the repair side.
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
    for pth_path in editable_ava_pth_paths(resolved_source):
        try:
            raw_target = pth_path.read_text().strip()
        except OSError:
            violations.append(f"{pth_path} unreadable")
            continue
        if not _target_is_allowed(raw_target, normalized_allowed):
            violations.append(f"{pth_path} names {raw_target or '(empty)'!r}")
    for record in editable_direct_url_paths(resolved_source):
        try:
            raw_text = record.read_text()
        except OSError:
            violations.append(f"{record} unreadable")
            continue
        if not _direct_url_is_allowed(raw_text, allowed_urls):
            violations.append(f"{record} records {raw_text.strip() or '(empty)'!r}")
    return tuple(sorted(violations))


def current_interpreter_source_root() -> Path | None:
    """Return this interpreter's checkout root when it is under ``.venv``.

    The supported layouts are ``<root>/.venv/bin/python`` and
    ``<root>/.venv/Scripts/python.exe``. A system interpreter or shim carries
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
    if venv.name != ".venv" or interpreter.parent.name not in {"bin", "Scripts"}:
        return None
    return source_root.resolve(strict=False)


def guard_editable_install(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    """Report and repair a poisoned install before an exec child can import it.

    The default exact-root allowance for ``~/Ava`` mirrors converge: a
    long-lived dev clone may be a legal editable target, while its disposable
    worktree descendants remain disallowed. Repair is independent of telemetry
    delivery so an event-contract drift cannot block the import recovery.
    """

    effective_allowed_roots = tuple(allowed_roots) or (Path.home() / "Ava",)
    violations = editable_install_violations(
        source_root,
        allowed_roots=effective_allowed_roots,
    )
    if not violations:
        return ()
    resolved_source = source_root.expanduser().resolve(strict=False)
    repair_editable_install(resolved_source, allowed_roots=effective_allowed_roots)
    try:
        shared.telemetry.emit(
            "telemetry",
            "exec_editable_install_poisoned",
            level="warning",
            source="exec_guard",
            attributes={
                "violations": list(violations),
                "source_root": str(resolved_source),
                "python": sys.executable,
            },
        )
    except Exception:
        logger.opt(exception=True).warning(
            "exec editable-install repair completed but telemetry emission failed",
            event="exec_editable_install_poisoned",
        )
    return violations
