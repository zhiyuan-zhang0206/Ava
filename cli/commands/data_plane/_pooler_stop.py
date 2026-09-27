"""One native PgBouncer stop owner, including retained shutdown attempts.

PgBouncer escalates a second SIGINT or SIGTERM to immediate shutdown. Persist
the exact native birth before the first signal; retries only wait for its exit.
"""

from __future__ import annotations

import configparser
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil

from shared.cluster import ownership
from shared.native_process.ownership import OwnedProcess
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import write_private_bytes


def _native_birth(identity: OwnedProcess) -> dict[str, int | float | None]:
    # Linux custody is the kernel starttime tick, independent of corrections
    # to the wall-clock timestamp reported by separate process readers.
    return {
        "pid": identity.pid,
        "birth": identity.birth if identity.starttime is None else None,
        "starttime": identity.starttime,
    }


@dataclass(frozen=True)
class OwnedPooler:
    identity: OwnedProcess
    port: int
    config: Path

    @classmethod
    def from_config(cls, identity: OwnedProcess, config: Path) -> OwnedPooler:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(config.read_text())
        port = parser.getint("pgbouncer", "listen_port")
        if not 0 < port < 65536:
            raise ValueError("invalid owned PgBouncer listen port")
        return cls(identity, port, config)

    def _stop_requested(self) -> bool:
        path = self.config.with_name("stop-intent.json")
        if not path.exists():
            return False
        raw: object = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise TypeError("invalid PgBouncer stop intent; custody retained")
        value = cast("dict[str, object]", raw)
        if set(value) != {"pid", "birth", "starttime"} or type(value["pid"]) is not int:
            raise RuntimeError("invalid PgBouncer stop intent; custody retained")
        valid_birth = (value["starttime"] is None and type(value["birth"]) in (int, float)) or (
            type(value["starttime"]) is int and value["birth"] is None
        )
        if not valid_birth:
            raise RuntimeError("invalid PgBouncer stop intent; custody retained")
        return value == _native_birth(self.identity)

    def require_accepting(self) -> None:
        """Preparation cannot rewrite or reload an already-stopping native birth."""
        ownership.require_listener(self.identity, self.port)
        if self._stop_requested():
            raise RuntimeError("PgBouncer stop was already requested; custody retained")

    def _process(self) -> psutil.Process:
        process = psutil.Process(self.identity.pid)
        current = _native_birth(OwnedProcess.capture(process))
        if current != _native_birth(self.identity) or not self.identity.live():
            raise RuntimeError("PgBouncer native birth changed before signal")
        return process

    def _request_stop(self, deadline: float) -> None:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("PgBouncer stop deadline expired; custody retained")
        with file_lock(self.config.with_name("stop-intent.lock"), timeout_s=budget):
            if not self.identity.live():
                return
            listeners = ownership.require_listener(self.identity, self.port, required=False)
            requested = self._stop_requested()
            admitted = bool(listeners) and not requested
            # Record before signalling, including when a previous native caller
            # already closed the listeners. Ambiguous delivery never authorizes a retry.
            if not requested:
                write_private_bytes(
                    self.config.with_name("stop-intent.json"),
                    json.dumps(_native_birth(self.identity)).encode(),
                )
            if admitted:
                if time.monotonic() >= deadline:
                    raise TimeoutError("PgBouncer stop deadline expired; custody retained")
                self._process().send_signal(signal.SIGINT)

    def _wait(self, deadline: float) -> bool:
        while self.identity.live():
            budget = deadline - time.monotonic()
            if budget <= 0:
                return False
            time.sleep(min(0.05, budget))
        return True

    def stop(self, *, deadline: float, force: bool = False) -> bool:
        """Request safe shutdown once; explicit force has a separate settle bound."""
        if not self.identity.live():
            return True
        try:
            self._request_stop(deadline)
        except (TimeoutError, LockTimeoutError):
            if not force:
                raise
        else:
            if self._wait(deadline):
                return True
        if not force:
            return False
        if not self.identity.live():
            return True
        ownership.require_listener(self.identity, self.port, required=False)
        self._process().kill()
        return self._wait(time.monotonic() + 0.5)
