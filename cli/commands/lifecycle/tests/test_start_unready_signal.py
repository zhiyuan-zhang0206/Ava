"""A non-critical service that misses its readiness window emits `service_start_unready`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli.commands._probe as _probe_commands
from cli.commands._repo import ServiceSpec
from cli.commands.lifecycle import start as start_commands
from ops.roster.service_spec import _GATEWAY


def _spec(session: str) -> ServiceSpec:
    return ServiceSpec(session=session, cmd="x", capabilities=_GATEWAY, requires_db=True)


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def emit(category: str, event_name: str, **kwargs: object) -> None:
        assert (category, event_name) == ("telemetry", "service_start_unready")
        assert kwargs["level"] == "warning"
        attributes = kwargs["attributes"]
        assert isinstance(attributes, dict)
        rows.append(dict(attributes))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_probe_commands.telemetry, "emit", emit)
    return rows


def test_one_event_per_non_critical_service(emitted: list[dict[str, object]]) -> None:
    _probe_commands._report_non_critical_unready_services((_spec("labeler"), _spec("watcher")))

    assert emitted == [{"service": "ava-labeler"}, {"service": "ava-watcher"}]


def test_readiness_verdict_emits_for_non_critical_only_and_still_fails_the_start(
    emitted: list[dict[str, object]],
) -> None:
    launch = SimpleNamespace(failed=())
    wait = SimpleNamespace(unready=(), non_critical_unready=(_spec("labeler"),))

    assert (
        start_commands._readiness_verdict(launch, wait)
        == start_commands.SERVICES_NOT_READY_EXIT_CODE
    )
    assert emitted == [{"service": "ava-labeler"}]


def test_a_ready_start_emits_nothing(emitted: list[dict[str, object]]) -> None:
    launch = SimpleNamespace(failed=())
    wait = SimpleNamespace(unready=(), non_critical_unready=())

    assert start_commands._readiness_verdict(launch, wait) is None
    assert emitted == []
