"""The stop window: a hierarchy tick opens no connection and claims no job while the unit is
quiesced."""

from __future__ import annotations

import pytest

from base.db.tests.fakes import fake_database
from base.deploy.maintenance import admission
from services.hierarchy_worker import runner
from services.hierarchy_worker.tests.slices import hierarchy_config


def test_a_quiesced_unit_ticks_without_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: True)
    dialed: list[object] = []

    def record_dial(**kw: object) -> None:
        dialed.append(kw)

    runner.run_tick(hierarchy_config(hierarchy_worker_enabled=True), fake_database(record_dial))

    assert dialed == []


def test_an_unquiesced_unit_ticks_through_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the same tick does dial when the unit is not quiesced."""
    monkeypatch.setattr(admission, "quiesced", lambda: False)
    dialed: list[object] = []

    def dial(**kw: object) -> None:
        dialed.append(kw)
        raise RuntimeError("stop here")

    runner.run_tick(
        hierarchy_config(hierarchy_worker_enabled=True), fake_database(dial)
    )  # the transient failure ends the tick

    assert dialed
