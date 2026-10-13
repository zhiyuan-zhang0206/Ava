"""Operator database composition, without a process-wide exemption switch."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, NoReturn

from base.cluster.machine import MachineIdentity
from base.telemetry import DrainResult, EventPipeline

__all__ = [
    "OperatorDatabaseFactory",
    "OperatorEventPipeline",
    "operator_database_factory",
    "operator_event_pipeline",
]


class OperatorDatabaseFactory:
    """One operator gate for normal handles and URL-specific recovery handles."""

    def __init__(self, *, local_host: Callable[[], str]) -> None:
        self._local_host = local_host
        self._lock = Lock()
        self._gate: Any = None

    def _admission_gate(self) -> Any:
        from base.db.code_version_gate import ProcessDbGate

        with self._lock:
            if self._gate is None:
                self._gate = ProcessDbGate(version=_unavailable_version, process="cli", exempt=True)
        return self._gate

    def __call__(self) -> Any:
        from base.cluster.dataplane.direct_url import ClusterDirectUrl
        from base.db import Database

        return Database.from_settings(
            gate=self._admission_gate(), direct_url=ClusterDirectUrl(self._local_host)
        )

    def for_url(self, url: str) -> Any:
        from dataclasses import replace

        from base.cluster.dataplane.direct_url import ClusterDirectUrl
        from base.db import Database
        from base.db.config import db_config_from_settings

        return Database(
            replace(db_config_from_settings(), db_url=url),
            gate=self._admission_gate(),
            direct_url=ClusterDirectUrl(self._local_host),
        )


def _unavailable_version() -> NoReturn:
    raise RuntimeError("an exempt operator database must not resolve a code version")


def operator_database_factory(*, local_host: Callable[[], str]) -> OperatorDatabaseFactory:
    """Construct without configuration or Git; each handle reads live settings."""
    return OperatorDatabaseFactory(local_host=local_host)


class OperatorEventPipeline:
    """Retain one lazy command writer and its finite shutdown result."""

    def __init__(
        self,
        database_factory: OperatorDatabaseFactory,
        *,
        machine: MachineIdentity,
        home: Path,
        flush_interval_s: float = 0.5,
    ) -> None:
        self._machine = machine
        self._home = home
        self._flush_interval_s = flush_interval_s
        self._database_factory = database_factory
        self._pipeline: EventPipeline | None = None

    def __call__(self) -> EventPipeline:
        if self._pipeline is None:
            from base.telemetry import build_pipeline

            self._pipeline = build_pipeline(
                database=self._database_factory,
                machine=self._machine,
                home=self._home,
                flush_interval_s=self._flush_interval_s,
            )
        return self._pipeline

    def initialize_logging(self, name: str) -> None:
        """Bind logging to this command's lazy writer and identity reader."""
        from base.telemetry.process_logging import init_cli_process

        init_cli_process(name=name, producer=self, machine_reader=self._machine.name)

    def close_command(self, *, primary: BaseException | None) -> None:
        """Report unfinished shutdown and preserve the command's primary error."""
        try:
            result = self.close(timeout=5.0)
            if result is not None:
                from base.telemetry import DrainStatus

                if result.status is DrainStatus.UNFINISHED:
                    print(
                        "ava: command telemetry writer did not finish before exit", file=sys.stderr
                    )
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"command telemetry shutdown failed: {cleanup!r}")

    def close(self, timeout: float = 5.0) -> DrainResult | None:
        """Stop only an existing writer; retain unfinished handles and errors."""
        if self._pipeline is None:
            return None
        return self._pipeline.stop(timeout=timeout)


def operator_event_pipeline(
    database_factory: OperatorDatabaseFactory, *, machine: MachineIdentity, home: Path
) -> OperatorEventPipeline:
    """Construct a lazy command owner without opening a writer or database."""
    return OperatorEventPipeline(database_factory, machine=machine, home=home)
