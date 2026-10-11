"""Operator database composition, without a process-wide exemption switch."""

from __future__ import annotations

from threading import Lock
from typing import Any, NoReturn

from base.telemetry import DrainResult, EventPipeline

__all__ = [
    "OperatorDatabaseFactory",
    "OperatorEventPipeline",
    "operator_database_factory",
    "operator_event_pipeline",
    "operator_machine_name",
]


class OperatorDatabaseFactory:
    """One operator gate for normal handles and URL-specific recovery handles."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._gate: Any = None

    def _admission_gate(self) -> Any:
        from base.db.code_version_gate import ProcessDbGate

        with self._lock:
            if self._gate is None:
                self._gate = ProcessDbGate(version=_unavailable_version, process="cli", exempt=True)
        return self._gate

    def __call__(self) -> Any:
        from base.db import Database

        return Database.from_settings(gate=self._admission_gate())

    def for_url(self, url: str) -> Any:
        from dataclasses import replace

        from base.db import Database
        from base.db.config import db_config_from_settings

        return Database(replace(db_config_from_settings(), db_url=url), gate=self._admission_gate())


def _unavailable_version() -> NoReturn:
    raise RuntimeError("an exempt operator database must not resolve a code version")


def operator_database_factory() -> OperatorDatabaseFactory:
    """Construct without configuration or Git; each handle reads live settings."""
    return OperatorDatabaseFactory()


class OperatorEventPipeline:
    """Retain one lazy command writer and its finite shutdown result."""

    def __init__(self, database_factory: OperatorDatabaseFactory) -> None:
        self._database_factory = database_factory
        self._pipeline: EventPipeline | None = None

    def __call__(self) -> EventPipeline:
        if self._pipeline is None:
            from base.telemetry import build_pipeline

            self._pipeline = build_pipeline(database=self._database_factory)
        return self._pipeline

    def close(self, timeout: float = 5.0) -> DrainResult | None:
        """Stop only an existing writer; retain unfinished handles and errors."""
        if self._pipeline is None:
            return None
        return self._pipeline.stop(timeout=timeout)


def operator_event_pipeline(database_factory: OperatorDatabaseFactory) -> OperatorEventPipeline:
    """Construct a lazy command owner without opening a writer or database."""
    return OperatorEventPipeline(database_factory)


def operator_machine_name() -> str:
    """Read the admitted operator's machine when telemetry needs its identity."""
    from base.cluster.machine import machine_name

    return machine_name()
