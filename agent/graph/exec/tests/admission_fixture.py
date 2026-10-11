"""Exec-owner admission synchronization for the cross-component force race proof."""

import threading
from pathlib import Path

import psycopg
import pytest

from base.agents.incarnation.exec_owner_protocol import OwnerReady
from base.agents.incarnation.resources import ResourceProcess
from base.agents.incarnation.tests.test_resources import _admitted
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.fixture
def admitted_owner_ready(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RuntimeIncarnation, threading.Event, threading.Event]:
    """Admit the real target and pause only after native readiness validation.

    The test's force writer starts after fixture setup. Readiness releases that
    writer, then native launch waits for its force transaction to finish. The
    real validator remains the first operation, so no native identity check is
    bypassed by this synchronization fixture.
    """
    from agent.graph.exec import _owned_run

    target = _admitted(db_conn)

    original_validate = _owned_run.validate_native_ready
    ready = threading.Event()
    force_done = threading.Event()

    def validate_then_wait(
        receipt: OwnerReady,
        launcher: ResourceProcess,
        context_path: Path,
    ) -> None:
        original_validate(receipt, launcher, context_path)
        ready.set()
        assert force_done.wait(10)

    monkeypatch.setattr(_owned_run, "validate_native_ready", validate_then_wait)
    return target, ready, force_done
