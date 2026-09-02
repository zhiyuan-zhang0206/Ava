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

A failed sync that removes either the pointer or its ``direct_url.json``
metadata is a half-uninstall, not a missing-install no-op. The repair guard
recreates a missing record when its sibling still identifies the install before
converge or an exec child proceeds.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import shared.telemetry
from shared.log import logger

EDITABLE_PTH_NAME = "_editable_impl_ava.pth"
EDITABLE_DIST_INFO_GLOB = "ava-*.dist-info/direct_url.json"
_IMPORT_GATE_TIMEOUT_S = 60
_IMPORT_GATE_CODE = "import agent.exec_child; print(agent.exec_child.__file__)"


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


def _missing_editable_direct_url_paths(source_root: Path) -> tuple[Path, ...]:
    """Missing direct-URL files under an existing sibling Ava dist-info dir.

    A pointer with no dist-info directory at all is reported but not repaired:
    this module cannot know the distribution version needed to fabricate a
    directory. A later verified ``uv sync`` owns that recovery.
    """

    missing: list[Path] = []
    for pth_path in _pth_paths_without_sibling_direct_url(source_root):
        dist_infos = sorted(
            (path for path in pth_path.parent.glob("ava-*.dist-info") if path.is_dir()),
            key=str,
        )
        if dist_infos:
            missing.append(dist_infos[0] / "direct_url.json")
    return tuple(missing)


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


def _allowed_pth_target(raw_text: str, allowed_roots: frozenset[str]) -> str | None:
    """Return the one allowed pointer target represented by ``raw_text``.

    The line is retained rather than replaced with a normalized spelling: an
    allowlisted stable clone remains its own legal target. Callers separately
    enforce the on-disk canonical form (one target line plus one newline).
    """

    target = raw_text.strip()
    if not target or len(target.splitlines()) != 1:
        return None
    try:
        normalized = _normalized_exact_path(Path(target))
    except (OSError, ValueError):
        return None
    return target if normalized in allowed_roots else None


def _canonical_pth_target(raw_text: str, allowed_roots: frozenset[str]) -> str | None:
    """Return a legal target only when the pointer has its canonical bytes."""

    target = _allowed_pth_target(raw_text, allowed_roots)
    if target is None or raw_text != f"{target}\n":
        return None
    return target


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
    """Repair missing or non-canonical pointers to an allowed exact root.

    An allowlisted clone root is legal only as that exact path. Its
    ``.worktrees/*`` descendants remain disposable and are repaired. A missing
    pointer beside an existing Ava ``direct_url.json`` is a failed-sync
    half-uninstall and is recreated.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    normalized_allowed = frozenset(
        {_normalized_exact_path(resolved_source)}
        | {_normalized_exact_path(root) for root in allowed_roots}
    )
    pending_repairs: dict[Path, tuple[str, str]] = {
        path: ("(missing)", str(resolved_source))
        for path in _missing_editable_ava_pth_paths(resolved_source)
    }
    for pth_path in editable_ava_pth_paths(resolved_source):
        raw_text = pth_path.read_text()
        if _canonical_pth_target(raw_text, normalized_allowed) is not None:
            continue
        target = _allowed_pth_target(raw_text, normalized_allowed) or str(resolved_source)
        pending_repairs[pth_path] = (raw_text.strip() or "(empty)", target)
    repairs: list[EditableInstallRepair] = []
    for pth_path, (poisoned_target, target) in sorted(
        pending_repairs.items(), key=lambda item: str(item[0])
    ):
        with editable_pth_write_window(resolved_source):
            _atomic_write_text(pth_path, f"{target}\n")
        repair = EditableInstallRepair(
            path=pth_path,
            poisoned_target=poisoned_target,
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
                "poisoned_target": poisoned_target,
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
    pending_repairs = dict.fromkeys(
        _missing_editable_direct_url_paths(resolved_source), "(missing)"
    )
    for path in editable_direct_url_paths(resolved_source):
        raw_text = path.read_text()
        if _direct_url_is_allowed(raw_text, allowed_urls):
            continue
        pending_repairs[path] = raw_text.strip() or "(empty)"
    repairs: list[EditableInstallRepair] = []
    for path, poisoned_target in sorted(pending_repairs.items(), key=lambda item: str(item[0])):
        repaired = json.dumps(
            {"url": resolved_source.as_uri(), "dir_info": {"editable": True}},
            separators=(",", ":"),
        )
        with editable_pth_write_window(resolved_source):
            _atomic_write_text(path, repaired)
        repair = EditableInstallRepair(
            path=path,
            poisoned_target=poisoned_target,
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
                "poisoned_target": poisoned_target,
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

    Returns an empty tuple when every record is canonical. Both editable
    records missing is a no-op, while a missing pointer or metadata beside its
    existing sibling is a half-uninstall violation repaired by the write side
    when the sibling supplies enough information.
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
        if _canonical_pth_target(raw_text, normalized_allowed) is None:
            target = raw_text.strip() or "(empty)"
            violations.append(
                f"{pth_path} names {target!r}; non-canonical editable pointer "
                "(expected a single allowed <root> line with trailing newline)"
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


def _venv_python(source_root: Path) -> Path | None:
    """First existing checkout virtualenv interpreter across supported layouts."""

    venv = source_root / ".venv"
    for candidate in (
        venv / "bin" / "python3",
        venv / "bin" / "python",
        venv / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def editable_console_script_violations(source_root: Path) -> tuple[str, ...]:
    """Report a missing Ava console script for the virtualenv's own layout."""

    interpreter = _venv_python(source_root)
    if interpreter is None:
        return ()
    script_name = "ava.exe" if interpreter.parent.name == "Scripts" else "ava"
    console_script = interpreter.with_name(script_name)
    if console_script.exists():
        return ()
    return (f"{console_script} editable console script missing",)


def _stderr_tail(stderr: str | bytes | None) -> str:
    """Keep import-gate diagnostics useful without flooding lifecycle output."""

    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return stderr[-1000:].strip()


def editable_import_gate(source_root: Path) -> tuple[str, ...]:
    """Prove the checkout venv imports ``agent.exec_child`` through its pointer.

    The probe deliberately starts in the platform temp directory and removes
    ``VIRTUAL_ENV``/``PYTHONPATH`` from its environment. Neither the checkout
    cwd nor a parent virtualenv can therefore mask a broken editable install.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    interpreter = _venv_python(resolved_source)
    if interpreter is None:
        return ("venv python missing",)
    env = {
        key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV", "PYTHONPATH"}
    }
    try:
        result = subprocess.run(  # noqa: S603 — checkout venv interpreter, never user input
            [str(interpreter), "-I", "-c", _IMPORT_GATE_CODE],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=_IMPORT_GATE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            "editable import gate failed "
            f"(rc=timeout; stderr={_stderr_tail(exc.stderr)!r}; path='')",
        )
    except OSError as exc:
        return (f"editable import gate failed (rc=spawn-error; stderr={str(exc)!r}; path='')",)

    output_lines = result.stdout.splitlines()
    path_text = output_lines[0].strip() if len(output_lines) == 1 else result.stdout.strip()
    diagnostic = (
        f"rc={result.returncode}; stderr={_stderr_tail(result.stderr)!r}; path={path_text!r}"
    )
    if result.returncode != 0 or len(output_lines) != 1 or not path_text:
        return (f"editable import gate failed ({diagnostic})",)
    try:
        reported_path = Path(path_text)
        if not reported_path.is_absolute():
            return (f"editable import gate failed ({diagnostic})",)
        imported_path = reported_path.resolve(strict=False)
    except (OSError, ValueError):
        return (f"editable import gate failed ({diagnostic})",)
    if not imported_path.is_relative_to(resolved_source):
        return (f"editable import gate failed ({diagnostic})",)
    return ()


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
