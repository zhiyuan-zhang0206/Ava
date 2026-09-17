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


def test_home_is_observability_station_marker_or_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider identity is either form: the lgtm-host marker OR the
    observability-station capability on this unit's own home."""
    from shared.machine import reset_identity, set_identity

    home = tmp_path / "station"
    home.mkdir()
    # The helper resolves the process home via shared.paths.ava_home.
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)

    # Capability form: the process home declares the station.
    set_identity(role="observability-station")
    try:
        assert observability.home_is_observability_station(home) is True
    finally:
        reset_identity()

    # Other capabilities are not the station.
    set_identity(role="gateway")
    try:
        assert observability.home_is_observability_station(home) is False
    finally:
        reset_identity()

    # Unconfigured unit (no capability) -> marker-only fallback.
    assert observability.home_is_observability_station(home) is False

    # Marker form wins even without any capability.
    (home / "lgtm-host").touch()
    assert observability.home_is_observability_station(home) is True


def test_home_is_observability_station_foreign_home_never_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A role-declared station's capability never leaks to another home (a dev
    worktree home on the same box must not inherit the prod station's identity)."""
    from shared.machine import reset_identity, set_identity

    own = tmp_path / "own"
    foreign = tmp_path / "foreign"
    own.mkdir()
    foreign.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: own)
    set_identity(role="observability-station")
    try:
        assert observability.home_is_observability_station(own) is True
        assert observability.home_is_observability_station(foreign) is False
    finally:
        reset_identity()
    (foreign / "lgtm-host").touch()
    assert observability.home_is_observability_station(foreign) is True


def test_collector_allowed_for_home_accepts_station_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway that declares the observability-station capability runs the
    local sidecar exactly like a marked host."""
    from shared.machine import reset_identity, set_identity

    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    set_identity(role="gateway,observability-station")
    try:
        assert observability.collector_allowed_for_home(home) is True
    finally:
        reset_identity()
    assert observability.collector_allowed_for_home(home) is False


# ── the read-boundary refusal predicate (2026-09-17) ───────────────────────
# The gateway's read gate refuses observability reads on a non-station home
# with no explicit Loki URL: 503 problem+json, code
# observability_read_unavailable. Readers (the self-evolution collect path,
# the weekly trigger) must recognize exactly this shape and no other.


class _RefusalResponse:
    """Duck-typed httpx.Response: status + parsed body (or a decode error)."""

    def __init__(self, status_code: int, payload: object, *, json_error: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error:
            raise ValueError("not json")
        return self._payload


_REFUSAL_BODY = {
    "type": "about:blank",
    "code": "observability_read_unavailable",
    "status": 503,
    "detail": "observability reads unavailable for this cluster; set AVA_TELEMETRY_LOKI_URL",
    "retryable": True,
}


def test_refusal_detail_matches_the_no_observability_problem() -> None:
    response = _RefusalResponse(503, _REFUSAL_BODY)
    assert observability.observability_refusal_detail(response) == _REFUSAL_BODY["detail"]


def test_refusal_detail_falls_back_to_the_default_detail() -> None:
    """A problem body without a usable detail still names the state."""
    response = _RefusalResponse(503, {"code": "observability_read_unavailable"})
    assert (
        observability.observability_refusal_detail(response)
        == "observability reads unavailable for this cluster"
    )


@pytest.mark.parametrize(
    "response",
    [
        _RefusalResponse(503, {"code": "loki_query_budget_unavailable"}),
        _RefusalResponse(503, "<html>a proxy's error page</html>"),
        _RefusalResponse(503, {}, json_error=True),
        _RefusalResponse(200, _REFUSAL_BODY),
        _RefusalResponse(500, _REFUSAL_BODY),
        object(),
    ],
    ids=[
        "other-503-code",
        "non-dict-body",
        "unparseable-body",
        "same-code-wrong-status",
        "same-code-500",
        "not-a-response",
    ],
)
def test_refusal_detail_is_none_for_every_other_shape(response: object) -> None:
    """Narrow on purpose: only 503 + the exact code count, so a transient
    outage (or a proxy page) is never mistaken for the policy state."""
    assert observability.observability_refusal_detail(response) is None
