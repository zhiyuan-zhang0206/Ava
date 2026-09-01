"""Operator commands for host-wide PTY allocation admission."""

from __future__ import annotations

import sys
from typing import TextIO

from shared.platform import LockTimeoutError
from shared.pty_sessions import allocation_freeze


def _print_freeze(
    snapshot: allocation_freeze.PtyAllocationFreeze, *, stream: TextIO | None = None
) -> None:
    if stream is None:
        stream = sys.stdout
    print(f"status={snapshot.status}", file=stream)
    if snapshot.generation is not None:
        print(f"generation={snapshot.generation}", file=stream)
    if snapshot.holder is not None:
        print(f"holder={snapshot.holder}", file=stream)
    if snapshot.reason is not None:
        print(f"reason={snapshot.reason}", file=stream)
    if snapshot.created_at is not None:
        print(f"created_at={snapshot.created_at.isoformat()}", file=stream)
    if snapshot.error is not None:
        print(f"error={snapshot.error}", file=stream)
    print(f"marker={allocation_freeze.state_path()}", file=stream)


def cmd_pty_freeze(*, holder: str, reason: str) -> int:
    try:
        snapshot = allocation_freeze.freeze(holder=holder, reason=reason)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"PTY allocation freeze refused: {exc}", file=sys.stderr)
        _print_freeze(allocation_freeze.read(), stream=sys.stderr)
        return 1
    _print_freeze(snapshot)
    return 0


def cmd_pty_status() -> int:
    snapshot = allocation_freeze.read()
    _print_freeze(snapshot)
    return 1 if snapshot.status == "invalid" else 0


def cmd_pty_resume(*, generation: str) -> int:
    try:
        resumed = allocation_freeze.resume(generation)
    except (
        LockTimeoutError,
        OSError,
        ValueError,
        allocation_freeze.InvalidPtyAllocationFreezeError,
    ) as exc:
        print(f"PTY allocation resume refused: {exc}", file=sys.stderr)
        _print_freeze(allocation_freeze.read(), stream=sys.stderr)
        return 1
    if not resumed:
        print(
            f"PTY allocation resume refused: generation {generation!r} does not own the freeze",
            file=sys.stderr,
        )
        _print_freeze(allocation_freeze.read(), stream=sys.stderr)
        return 1
    _print_freeze(allocation_freeze.read())
    return 0
