"""Only complete, consecutive healthy observations may promote a release."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cli.commands import _cluster_health
from tests.cli.test_cluster_health import (
    _all_checks_pass as _all_checks_pass,
)
from tests.cli.test_cluster_health import (
    _count_record,
    _read_count,
)
from tests.cli.test_cluster_health import (
    _home as _home,
)
from tests.cli.test_cluster_health import (
    _no_deploy_in_flight as _no_deploy_in_flight,
)
from tests.cli.test_cluster_health import (
    _provider_guard_healthy as _provider_guard_healthy,
)
from tests.cli.test_cluster_health import (
    _sent_alerts as _sent_alerts,
)


def test_pending_lkg_advances_after_two_fully_healthy_passes(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new pin becomes LKG only after two consecutive healthy observations."""
    promoted: list[float] = []
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def _promote(*, min_age_s: float) -> bool:
        promoted.append(min_age_s)
        return True

    monkeypatch.setattr("shared.cluster_pin.promote_pending_known_good_if_ready", _promote)

    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]

    assert _cluster_health.run_health_probe() == 0
    assert promoted == [_cluster_health.PENDING_LKG_MIN_AGE_S]
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["service", "disk", "provider"])
def test_degraded_health_resets_lkg_without_triggering_rollback(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """An alert-only outage still disqualifies a last-known-good candidate."""
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def unexpected_promotion(*, min_age_s: float) -> bool:
        pytest.fail("Degraded release was promoted")

    monkeypatch.setattr(
        "shared.cluster_pin.promote_pending_known_good_if_ready", unexpected_promotion
    )
    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE
    assert marker.exists()
    (_home / _cluster_health.FAILURE_COUNT_FILE).write_text(_count_record(2))

    with monkeypatch.context() as degraded:
        if failure == "service":
            degraded.setattr(_cluster_health, "_service_probes", lambda: ["frontend (unknown)"])
        elif failure == "disk":
            degraded.setattr(_cluster_health, "_disk_usage_failure", lambda: "disk full")
        else:

            def provider_failure(_path: Path, *, alert_failure: object) -> int:
                return 1

            degraded.setattr(_cluster_health, "run_provider_guard", provider_failure)
        assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert not marker.exists()
    assert _read_count(_home).splitlines()[0] == "0"
    assert _cluster_health.run_health_probe() == 0
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]


@pytest.mark.parametrize("failure", ["corrupt-selection", "interrupted-check"])
def test_incomplete_observation_resets_pending_lkg_streak(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failed observation cannot bridge two healthy passes into an LKG window."""
    from shared import service_selection

    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def unexpected_promotion(*, min_age_s: float) -> bool:
        pytest.fail("Incomplete observation contributed to LKG")

    monkeypatch.setattr(
        "shared.cluster_pin.promote_pending_known_good_if_ready", unexpected_promotion
    )
    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE

    def failed_classification(_minimum: int) -> str:
        service_selection.read_selection()
        raise AssertionError("invalid selection was accepted")

    def interrupted() -> list[str]:
        raise KeyboardInterrupt

    def no_agents(_minimum: int) -> bool:
        return False

    with monkeypatch.context() as broken:
        if failure == "corrupt-selection":
            broken.setattr(service_selection, "selection_path", lambda: _home / "selection.json")
            (_home / "selection.json").write_text("{")
            broken.setattr(_cluster_health, "_agent_population", no_agents)
            broken.setattr(
                _cluster_health, "_agent_population_failure_class", failed_classification
            )
            expected = ValueError
        else:
            broken.setattr(_cluster_health, "_service_probes", interrupted)
            expected = KeyboardInterrupt
        with pytest.raises(expected):
            _cluster_health.run_health_probe()
    assert not marker.exists()
    assert _cluster_health.run_health_probe() == 0
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]


def test_gating_failure_resets_pending_lkg_streak(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any gating failure restarts the observation window, including environmental ones."""
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def _not_ready(*, min_age_s: float) -> bool:
        return False

    monkeypatch.setattr("shared.cluster_pin.promote_pending_known_good_if_ready", _not_ready)

    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE
    assert marker.exists()

    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: True)
    assert _cluster_health.run_health_probe() == 1
    assert not marker.exists()

    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert _cluster_health.run_health_probe() == 0
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]
