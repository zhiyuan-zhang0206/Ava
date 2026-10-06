"""Private staging and per-kind concurrency for scheduled backup workers."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from base.host.atomic_io import write_text_atomic
from base.native_process.os_platform import LockTimeoutError

TERMINATE_GRACE_S = 3.0


def no_business_staging(_work: Path) -> None:
    """The operation has no additional plaintext cleanup."""


@dataclass(frozen=True)
class OperationKind:
    """One scheduled job's private scratch root and plaintext cleanup."""

    name: str
    control_root: Path
    sanitize: Callable[[Path], None] = no_business_staging
    grace_s: float = TERMINATE_GRACE_S


class OperationDeferred(RuntimeError):  # noqa: N818 -- a clean scheduling outcome
    """The worker declined before preparing an artifact."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"operation deferred ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


class OperationBusyError(LockTimeoutError):
    """Another scheduled operation holds this kind's concurrency lock."""


def publish_result(path: Path, result: Mapping[str, object]) -> None:
    """Publish a complete private request or result, never a process receipt."""
    write_text_atomic(
        path,
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        mode=0o600,
        sync_parent=True,
    )


def cleanup(kind: OperationKind, work: Path) -> None:
    """Remove private staging; retain its path and raise if sanitization fails."""
    try:
        kind.sanitize(work)
        shutil.rmtree(work)
    except Exception as exc:
        exc.add_note(f"backup staging cleanup failed; inspect private directory {work}")
        raise
