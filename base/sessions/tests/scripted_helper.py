from __future__ import annotations

from pathlib import Path
from typing import Any

from base.host.converge.accessibility import AccessibilityStatus
from base.host.converge.screen_capture import ScreenCaptureStatus
from base.host.permissions_helper.client import (
    HelperAtSocket,
    HelperFactory,
    HelperShutdownResult,
    PermissionsHelperClient,
    PingResult,
    RootSeedConfig,
    RootStatus,
)

__all__ = ["ScriptedHelper", "scripted_at", "serve"]

_UNUSED_SOCKET = "/unused.sock"


class ScriptedHelper(PermissionsHelperClient):
    def __init__(self, **script: object) -> None:
        super().__init__(_UNUSED_SOCKET)
        self._script = script
        self.calls: list[str] = []

    def _answer(self, method: str, *args: object) -> Any:
        self.calls.append(method)
        if method not in self._script:
            raise AssertionError(f"the scripted helper was asked for unscripted {method!r}")
        answer = self._script[method]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(*args)
        return answer

    def ping(self) -> PingResult:
        return self._answer("ping")

    def root_status(self) -> RootStatus:
        return self._answer("root_status")

    def seed_root(self, config: RootSeedConfig) -> RootStatus:
        return self._answer("seed_root", config)

    def stop_root(self) -> RootStatus:
        return self._answer("stop_root")

    def shutdown_helper(self, run_dir: Path) -> HelperShutdownResult:
        return self._answer("shutdown_helper", run_dir)

    def check_screen_capture(self, *, settle_s: float = 0.0) -> ScreenCaptureStatus:
        return self._answer("check_screen_capture")

    def check_accessibility(self, *, settle_s: float = 0.0) -> AccessibilityStatus:
        return self._answer("check_accessibility")


def serve(helper: PermissionsHelperClient) -> HelperFactory:
    return lambda: helper


def scripted_at(helper: PermissionsHelperClient) -> HelperAtSocket:
    return lambda _socket_path: helper
