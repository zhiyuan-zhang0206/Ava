"""A non-critical service that misses its readiness window emits `service_start_unready`."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace

import pytest

import cli.commands.probe as _probe_commands
from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.lifecycle import start as start_commands
from services.supervision.roster.service_spec import GATEWAY, ServiceSpec


def _spec(session: str) -> ServiceSpec:
    return ServiceSpec(session=session, cmd="x", capabilities=GATEWAY, requires_db=True)


@pytest.fixture
def emitted(emitted_events: EmittedEvents) -> Generator[FollowedAttributes]:
    """The attributes of every `service_start_unready` event. After the test, every event that was
    emitted must be that event, at warning level."""
    yield emitted_events.following("service_start_unready")
    for event in emitted_events:
        assert (event.category, event.event_name) == ("telemetry", "service_start_unready")
        assert event.level == "warning"


def test_one_event_per_non_critical_service(emitted: FollowedAttributes) -> None:
    _probe_commands.report_non_critical_unready_services((_spec("labeler"), _spec("watcher")))

    assert emitted == [{"service": "ava-labeler"}, {"service": "ava-watcher"}]


def test_readiness_verdict_emits_for_non_critical_only_and_allows_the_start(
    emitted: FollowedAttributes,
) -> None:
    launch = SimpleNamespace(failed=())
    wait = SimpleNamespace(unready=(), non_critical_unready=(_spec("labeler"),))

    assert start_commands.readiness_verdict(launch, wait) is None
    assert emitted == [{"service": "ava-labeler"}]


def test_a_ready_start_emits_nothing(emitted: FollowedAttributes) -> None:
    launch = SimpleNamespace(failed=())
    wait = SimpleNamespace(unready=(), non_critical_unready=())

    assert start_commands.readiness_verdict(launch, wait) is None
    assert emitted == []


@pytest.mark.parametrize("failed, expected", [("ava-browser-mcp", None), ("ava-gateway", 4)])
def test_only_core_launch_failures_block_serving(
    emitted: FollowedAttributes, failed: str, expected: int | None
) -> None:
    launch = SimpleNamespace(failed=(failed,))
    wait = SimpleNamespace(unready=(), non_critical_unready=())
    assert start_commands.readiness_verdict(launch, wait) == expected
