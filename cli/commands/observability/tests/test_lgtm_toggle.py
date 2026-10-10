"""Observability toggles change intent through the sole start lifecycle."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.deploy.lifecycle.service_selection import ServiceSelection
from base.telemetry import EventPipeline
from cli.commands.observability import lgtm
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: ServiceSelection
) -> tuple[Path, list[dict[str, object]]]:
    marker = tmp_path / "lgtm-host"
    monkeypatch.setattr(lgtm, "lgtm_host_marker", lambda: marker)
    monkeypatch.setattr("base.deploy.lifecycle.service_selection.read_selection", lambda: selection)
    calls: list[dict[str, object]] = []

    def start(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", start)
    return marker, calls


def test_on_preserves_explicit_other_service_choices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, ServiceSelection("only", frozenset({"ops"})))
    assert (
        lgtm.cmd_lgtm_on(
            retained_children=[], database_factory=operator_database, producer=operator_pipeline
        )
        == 0
    )
    assert marker.exists()
    assert calls == [
        {"only_services": ("grafana", "loki", "ops", "prometheus"), "retained_children": []}
    ]


def test_off_disables_backends_even_on_role_declared_station(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, ServiceSelection("except", frozenset({"browser"})))
    marker.touch()
    data = tmp_path / "loki-data"
    data.write_bytes(b"durable history")
    assert (
        lgtm.cmd_lgtm_off(
            retained_children=[], database_factory=operator_database, producer=operator_pipeline
        )
        == 0
    )
    assert not marker.exists()
    assert calls == [
        {
            "disabled_services": ("browser", "grafana", "loki", "prometheus"),
            "all_services": False,
            "retained_children": [],
        }
    ]
    assert data.read_bytes() == b"durable history"


def test_off_only_backend_allowlist_does_not_enable_all_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from types import SimpleNamespace

    _marker, calls = _wire(
        monkeypatch, tmp_path, ServiceSelection("only", frozenset(lgtm.BACKENDS))
    )
    monkeypatch.setattr(
        "ops.roster.build_services",
        lambda: tuple(
            SimpleNamespace(session=n) for n in ("gateway", "loki", "prometheus", "grafana")
        ),
    )
    assert (
        lgtm.cmd_lgtm_off(
            retained_children=[], database_factory=operator_database, producer=operator_pipeline
        )
        == 0
    )
    assert calls == [
        {"disabled_services": ("gateway", "loki", "prometheus", "grafana"), "retained_children": []}
    ]


def test_normal_start_refusal_is_not_reported_as_toggle_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _wire(monkeypatch, tmp_path, ServiceSelection("except", frozenset()))

    def refuse(**_kwargs: object) -> int:
        return 1

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", refuse)
    assert (
        lgtm.cmd_lgtm_on(
            retained_children=[], database_factory=operator_database, producer=operator_pipeline
        )
        == 1
    )
