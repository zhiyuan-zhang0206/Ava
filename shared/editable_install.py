"""Guard the editable Ava install pointer in a checkout's virtualenv.

uv writes ``_editable_impl_ava.pth`` into the active virtualenv. A polluted
``VIRTUAL_ENV`` can therefore make a worktree install repoint a long-lived
checkout's interpreter at disposable source. Lifecycle callers use the repair
primitive for the prod checkout only; update callers use the write window so
an operator's emergency read-only mode does not block a legitimate sync.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path

import shared.telemetry

EDITABLE_PTH_NAME = "_editable_impl_ava.pth"


@dataclass(frozen=True)
class EditablePthRepair:
    """One poisoned pointer replaced with the checkout it belongs to."""

    pth_path: Path
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


def _normalized_exact_path(path: Path) -> str:
    """Platform-native exact path identity, including Windows case folding."""

    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _target_is_allowed(raw_target: str, allowed_roots: frozenset[str]) -> bool:
    if not raw_target or "\n" in raw_target or "\r" in raw_target:
        return False
    try:
        normalized = _normalized_exact_path(Path(raw_target))
    except (OSError, ValueError):
        return False
    return normalized in allowed_roots


@contextlib.contextmanager
def editable_pth_write_window(source_root: Path) -> Generator[None, None, None]:
    """Temporarily add owner-write to read-only Ava pointers, then restore it.

    The exact original mode is restored in ``finally`` on both successful and
    failed syncs. If uv atomically replaces the file, the replacement receives
    the original protection too.
    """

    original_modes: dict[Path, int] = {}
    for pth_path in editable_ava_pth_paths(source_root):
        mode = stat.S_IMODE(pth_path.stat().st_mode)
        if mode & stat.S_IWUSR:
            continue
        original_modes[pth_path] = mode
        pth_path.chmod(mode | stat.S_IWUSR)
    try:
        yield
    finally:
        for pth_path, mode in original_modes.items():
            with contextlib.suppress(FileNotFoundError):
                pth_path.chmod(mode)


def repair_editable_ava_pth(
    source_root: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[EditablePthRepair, ...]:
    """Repair pointers outside the exact source-root allowlist.

    An allowlisted clone root is legal only as that exact path. Its
    ``.worktrees/*`` descendants remain disposable and are repaired.
    """

    resolved_source = source_root.expanduser().resolve(strict=False)
    normalized_allowed = frozenset(
        {_normalized_exact_path(resolved_source)}
        | {_normalized_exact_path(root) for root in allowed_roots}
    )
    repairs: list[EditablePthRepair] = []
    for pth_path in editable_ava_pth_paths(resolved_source):
        raw_target = pth_path.read_text().strip()
        if _target_is_allowed(raw_target, normalized_allowed):
            continue
        with editable_pth_write_window(resolved_source):
            pth_path.write_text(str(resolved_source))
        repair = EditablePthRepair(
            pth_path=pth_path,
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
