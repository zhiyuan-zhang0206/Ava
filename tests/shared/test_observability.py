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

from shared import observability


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

    assert observability.collector_allowed_for_home(home) is expected


def test_collector_allowed_for_home_none_home_is_never_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gateway home (pure runner, unconfigured unit, bootstrap) keeps the
    relay collector regardless of the marker or the override."""
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert observability.collector_allowed_for_home(None) is True
