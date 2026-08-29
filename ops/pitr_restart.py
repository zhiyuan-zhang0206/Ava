"""Typed, non-secret continuation carried by the cluster restart seam."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PitrRestartContinuation:
    operation_id: str
    orchestration_id: str
    handoff_token: str
    action: Literal["activate", "rollback"] = "activate"
    expected_phase: str = "wal_restart_pending"
    expected_digest: str | None = None

    def origin(self) -> str:
        prefix = "pitr-activation" if self.action == "activate" else "pitr-rollback"
        return f"{prefix}:{self.operation_id}:{self.orchestration_id}"

    def resume_origin(self) -> str:
        return ":".join(
            (
                "restart-continuation",
                self.operation_id,
                self.orchestration_id,
                self.handoff_token,
                self.expected_phase,
                self.expected_digest or "none",
            )
        )


def resume_commands(
    continuation: PitrRestartContinuation | None,
    origin: str,
    native_arg: Callable[[str], str],
) -> tuple[str, str]:
    if continuation is None:
        return "", ""
    if continuation.origin() != origin:
        raise ValueError("PITR restart continuation differs from restart origin")
    value = continuation.resume_origin()
    if continuation.action == "activate":
        command = "ava cluster pitr activate --origin "
    else:
        command = "ava cluster pitr rollback --continuation "
    return " && " + command + shlex.quote(value), " && " + command + native_arg(value)
