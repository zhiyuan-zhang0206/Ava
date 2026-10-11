"""Cluster identity helpers — the otel-collector lifecycle decision.

``collector_allowed_for_home`` is the single predicate the roster gate, the
converge step, and the sidecar healthcheck share (issue #622's drift lesson):
the LGTM host always runs the sidecar, an explicit
``AVA_TELEMETRY_OTLP_ENDPOINT`` override opens it on any other gateway, and a
non-gateway unit (no gateway home) is never gated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.telemetry import observability
from base.telemetry.tests.identity_inputs import machine_identity


@pytest.mark.parametrize(
    ("marker", "endpoint_override", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_collector_allowed_for_home_marker_or_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker: bool,
    endpoint_override: bool,
    expected: bool,
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    if marker:
        (home / "lgtm-host").touch()
    if endpoint_override:
        monkeypatch.setitem(
            os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318"
        )
    else:
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)

    assert (
        observability.collector_allowed_for_home(home, machine=machine_identity(), unit_home=home)
        is expected
    )


def test_collector_allowed_for_home_none_home_is_never_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gateway home (pure runner, unconfigured unit, bootstrap) keeps the
    relay collector regardless of the marker or the override."""
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert (
        observability.collector_allowed_for_home(
            None, machine=machine_identity(), unit_home=Path("unused")
        )
        is True
    )


def test_home_is_observability_station_marker_or_capability(tmp_path: Path) -> None:
    home = tmp_path / "station"
    home.mkdir()
    assert observability.home_is_observability_station(
        home, machine=machine_identity(roles=frozenset({"observability-station"})), unit_home=home
    )
    assert not observability.home_is_observability_station(
        home, machine=machine_identity(roles=frozenset({"gateway"})), unit_home=home
    )
    assert not observability.home_is_observability_station(
        home, machine=machine_identity(roles=None), unit_home=home
    )
    (home / "lgtm-host").touch()
    assert observability.home_is_observability_station(
        home, machine=machine_identity(roles=None), unit_home=home
    )


def test_station_capability_never_leaks_to_foreign_home(tmp_path: Path) -> None:
    own, foreign = tmp_path / "own", tmp_path / "foreign"
    own.mkdir()
    foreign.mkdir()
    machine = machine_identity(roles=frozenset({"observability-station"}))
    assert observability.home_is_observability_station(own, machine=machine, unit_home=own)
    assert not observability.home_is_observability_station(foreign, machine=machine, unit_home=own)
    (foreign / "lgtm-host").touch()
    assert observability.home_is_observability_station(foreign, machine=machine, unit_home=own)


def test_collector_allowed_for_home_accepts_station_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    machine = machine_identity(roles=frozenset({"gateway", "observability-station"}))
    assert observability.collector_allowed_for_home(tmp_path, machine=machine, unit_home=tmp_path)
    assert not observability.collector_allowed_for_home(
        tmp_path, machine=machine_identity(roles=None), unit_home=tmp_path
    )
